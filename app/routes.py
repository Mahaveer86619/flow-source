import asyncio
import hashlib
import json
import logging
import os
import pathlib
import tempfile
import urllib.parse
from datetime import datetime, timedelta
from typing import List, Optional

import httpx
import ytmusicapi
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Request,
    Response,
    status,
)
from fastapi.responses import StreamingResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from .config import settings
from .database import get_db
from .models import (
    AddPlaylistItemsRequest,
    ArtistResponse,
    CreatePlaylistRequest,
    EditPlaylistRequest,
    FlowCollaboratorRequest,
    FlowPlaylistAddTrackRequest,
    FlowPlaylistCreateRequest,
    FlowPlaylistUpdateRequest,
    HistoryEntryResponse,
    HistoryResponse,
    HomeResponse,
    LibraryResponse,
    PlayHistory,
    Playlist,
    PlaylistCollaborator,
    PlaylistResponse,
    PlaylistTrack,
    RemovePlaylistItemsRequest,
    SongResponse,
    Token,
    User,
    UserCreate,
    UserLogin,
    UserResponse,
    UserSettingsUpdate,
    BrowserFrameResponse,
    BrowserKeyRequest,
    BrowserTapRequest,
    BrowserTypeRequest,
    YTCookiesPayload,
    YTOAuthResponse,
    YTOAuthStatus,
)
from .browser_session import browser_session
from .services import auth_service, extract_audio_url, yt_service
from .utils import (
    curl_to_headers,
    fix_thumbnail_url,
    normalize_artist,
    normalize_playlist,
    normalize_song,
    write_cookie_file,
)

router = APIRouter()
logger = logging.getLogger("flow.routes")


@router.get("/health")
async def health_check():
    if settings.DEBUG:
        logger.debug("Health check")
    return {"status": "ok"}


oauth2_scheme = OAuth2PasswordBearer(tokenUrl="v1/auth/login")

_shared_client: Optional[httpx.AsyncClient] = None


def get_shared_client():
    global _shared_client
    if _shared_client is None:
        _shared_client = httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, connect=10.0),
            follow_redirects=True,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )
    return _shared_client


async def close_shared_client():
    global _shared_client
    if _shared_client is not None:
        await _shared_client.aclose()
        _shared_client = None


async def get_current_user(
    token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)
):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        if settings.DEBUG:
            logger.debug("Decoding JWT token")
        payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
        )
        username: str = payload.get("sub")
        if username is None:
            logger.warning("JWT token missing 'sub' claim")
            raise credentials_exception
    except JWTError as e:
        logger.warning(f"JWT validation failed: {e}")
        raise credentials_exception

    user = db.query(User).filter(User.username == username).first()
    if user is None:
        logger.warning(f"User from JWT token not found: {username}")
        raise credentials_exception
    return user


def _require_yt_auth(user: User):
    """Raise 401 if the user has no YT credentials configured."""
    if not user.yt_auth_json:
        # Check if a global fallback exists (optional, based on services.py logic)
        if not os.path.exists(settings.AUTH_FILE_PATH):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="YT Music not connected. Please connect your account first.",
            )


def _handle_yt_error(e: Exception, username: str, context: str = "operation"):
    """Handle common ytmusicapi errors and return appropriate HTTP exceptions."""
    err_msg = str(e)
    if "Sign in" in err_msg or "twoColumnBrowseResultsRenderer" in err_msg:
        logger.warning(
            f"YT Music session expired for {username} during {context}: {err_msg}"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="YouTube Music session expired. Please reconnect your account.",
        )
    logger.error(f"YT Music {context} failed for {username}: {e}")
    raise HTTPException(500, err_msg)


# --- User Management Endpoints ---


@router.post("/auth/signup", response_model=UserResponse)
async def signup(user_in: UserCreate, db: Session = Depends(get_db)):
    if settings.DEBUG:
        logger.debug(
            f"Signup attempt for username: {user_in.username}, email: {user_in.email}"
        )
    else:
        logger.info(f"Signup attempt for username: {user_in.username}")

    db_user = db.query(User).filter(User.username == user_in.username).first()
    if db_user:
        logger.warning(f"Signup failed: Username {user_in.username} already registered")
        raise HTTPException(status_code=400, detail="Username already registered")

    hashed_password = auth_service.get_password_hash(user_in.password)
    user_code = auth_service.generate_user_code(user_in.username, db)
    new_user = User(
        username=user_in.username,
        email=user_in.email,
        hashed_password=hashed_password,
        user_code=user_code,
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    logger.info(
        f"User signed up successfully: {new_user.username} ({new_user.user_code})"
    )

    response = UserResponse.model_validate(new_user)
    response.has_yt_auth = bool(new_user.yt_auth_json)
    if new_user.settings_json:
        response.settings = json.loads(new_user.settings_json)
    return response


@router.post("/auth/login", response_model=Token)
async def login(
    db: Session = Depends(get_db), form_data: OAuth2PasswordRequestForm = Depends()
):
    if settings.DEBUG:
        logger.debug(f"Login attempt for username: {form_data.username}")

    user = db.query(User).filter(User.username == form_data.username).first()
    if not user or not auth_service.verify_password(
        form_data.password, user.hashed_password
    ):
        logger.warning(f"Login failed for username: {form_data.username}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    logger.info(f"User logged in successfully: {user.username}")

    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = auth_service.create_access_token(
        data={"sub": user.username}, expires_delta=access_token_expires
    )
    return {"access_token": access_token, "token_type": "bearer"}


@router.get("/auth/me", response_model=UserResponse)
async def read_users_me(current_user: User = Depends(get_current_user)):
    response = UserResponse.model_validate(current_user)
    response.has_yt_auth = bool(current_user.yt_auth_json)
    response.yt_name = current_user.yt_name
    response.yt_avatar_url = current_user.yt_avatar_url
    if current_user.settings_json:
        response.settings = json.loads(current_user.settings_json)
    return response


@router.post("/auth/refresh-profile", response_model=UserResponse)
async def refresh_user_profile(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    try:
        profile = yt_service.get_user_profile(current_user)
        if profile:
            current_user.yt_name = profile.get("name")
            current_user.yt_avatar_url = profile.get("avatar")
            db.add(current_user)
            db.commit()
            db.refresh(current_user)
    except Exception as e:
        logger.error(f"Failed to refresh profile: {e}")

    response = UserResponse.model_validate(current_user)
    response.has_yt_auth = bool(current_user.yt_auth_json)
    if current_user.settings_json:
        response.settings = json.loads(current_user.settings_json)
    return response


@router.patch("/auth/settings", response_model=UserResponse)
async def update_settings(
    req: UserSettingsUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    current_user.settings_json = json.dumps(req.settings)
    db.add(current_user)
    db.commit()
    db.refresh(current_user)

    response = UserResponse.model_validate(current_user)
    response.has_yt_auth = bool(current_user.yt_auth_json)
    response.settings = req.settings
    return response


# --- Home & Feed Endpoints ---


def get_proxy_base(request: Request) -> str:
    if settings.PROXIED_IMAGE_URL:
        return settings.PROXIED_IMAGE_URL

    # Use the request's own base URL to build the proxy endpoint
    # Base URL should include protocol and host
    base_url = f"{request.url.scheme}://{request.url.netloc}"

    # The router is prefixed with /v1, so the endpoint is at /v1/proxy-image
    return f"{base_url}/v1/proxy-image"


@router.get("/home", response_model=HomeResponse)
async def get_home(
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    limit: int = 25,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "public, max-age=300"
    logger.debug(
        f"Requesting home data for user: {current_user.username} with limit: {limit}"
    )
    try:
        proxy_base = get_proxy_base(request)
        # 1. Fetch from Cache (or compute if miss)
        data = await yt_service.get_home_cached(
            db, current_user, limit, proxy_base=proxy_base
        )

        # 2. Trigger background warm up for next time (proactive)
        background_tasks.add_task(
            yt_service.warm_up_user_cache, db, current_user, proxy_base
        )

        # For backward compatibility with specific endpoints/legacy parsers
        quick_picks = []
        listening_again = []
        forgotten_favorites = []
        music_for_you = []
        trending_artists = []

        for shelf in data.shelves:
            section = shelf.get("section")

            # Map songs
            song_items = [
                item["data"]
                for item in shelf.get("items", [])
                if item["type"] == "song"
            ]
            if section == "quickPicks":
                quick_picks.extend(song_items)
            elif section == "listeningAgain":
                listening_again.extend(song_items)
            elif section == "forgottenFavorites":
                forgotten_favorites.extend(song_items)
            elif section == "musicForYou":
                music_for_you.extend(song_items)

            # Map artists for trendingArtists
            if section == "trending":
                artist_items = [
                    item["data"]
                    for item in shelf.get("items", [])
                    if item["type"] == "artist"
                ]
                trending_artists.extend(artist_items)

        data.quickAccess = quick_picks
        data.listeningAgain = listening_again
        data.forgottenFavorites = forgotten_favorites
        data.musicForYou = music_for_you
        data.trendingArtists = trending_artists
        data.freshFinds = [
            item["data"]
            for shelf in data.shelves
            if shelf.get("section") == "freshFinds"
            for item in shelf.get("items", [])
            if item["type"] == "song"
        ]

        return data
    except Exception as e:
        logger.exception(f"Error in get_home for user {current_user.username}: {e}")
        raise HTTPException(500, str(e))


@router.post("/artists/{channel_id}/like")
async def like_artist(channel_id: str, current_user: User = Depends(get_current_user)):
    _require_yt_auth(current_user)
    try:
        # ytmusicapi uses subscribe/unsubscribe for artists
        res = yt_service.get_client(current_user).subscribe_artists([channel_id])
        return {"status": res}
    except Exception as e:
        _handle_yt_error(e, current_user.username, "liking artist")


@router.post("/artists/{channel_id}/unlike")
async def unlike_artist(
    channel_id: str, current_user: User = Depends(get_current_user)
):
    _require_yt_auth(current_user)
    try:
        res = yt_service.get_client(current_user).unsubscribe_artists([channel_id])
        return {"status": res}
    except Exception as e:
        _handle_yt_error(e, current_user.username, "unliking artist")


@router.get("/home/quick-access", response_model=List[SongResponse])
async def quick_access(current_user: User = Depends(get_current_user)):
    return (await get_home(current_user=current_user)).quickAccess


@router.get("/home/listening-again", response_model=List[SongResponse])
async def listening_again(current_user: User = Depends(get_current_user)):
    return (await get_home(current_user=current_user)).listeningAgain


@router.get("/home/forgotten-favorites", response_model=List[SongResponse])
async def forgotten_favorites(current_user: User = Depends(get_current_user)):
    return (await get_home(current_user=current_user)).forgottenFavorites


@router.get("/home/music-for-you", response_model=List[SongResponse])
async def music_for_you(current_user: User = Depends(get_current_user)):
    return (await get_home(current_user=current_user)).musicForYou


@router.get("/home/trending-artists", response_model=List[ArtistResponse])
async def trending_artists(current_user: User = Depends(get_current_user)):
    return (await get_home(current_user=current_user)).trendingArtists


@router.delete("/home/cache")
async def clear_home_cache(current_user: User = Depends(get_current_user)):
    logger.info(f"Clearing home cache for user: {current_user.username}")
    yt_service.clear_cache(current_user.id)
    return {"status": "ok", "message": "Your home cache cleared"}


@router.get("/feed", response_model=HomeResponse)
async def get_feed(request: Request, db: Session = Depends(get_db)):
    try:
        proxy_base = get_proxy_base(request)
        return yt_service.get_feed_cached(db, proxy_base=proxy_base)
    except Exception as e:
        raise HTTPException(500, str(e))


# --- Search Endpoints ---


@router.get("/search/songs", response_model=List[SongResponse])
async def search_songs(
    request: Request,
    q: str,
    limit: int = 20,
    current_user: User = Depends(get_current_user),
):
    if not q.strip():
        raise HTTPException(400, "Query is empty")
    try:
        proxy_base = get_proxy_base(request)
        results = yt_service.get_client(current_user).search(
            q, filter="songs", limit=limit
        )
        return [s for item in results if (s := normalize_song(item, proxy_base))]
    except Exception as e:
        _handle_yt_error(e, current_user.username, f"searching for '{q}'")


@router.get("/search/suggestions")
async def get_search_suggestions(
    q: str, current_user: User = Depends(get_current_user)
):
    try:
        return yt_service.get_client(current_user).get_search_suggestions(q)
    except Exception as e:
        _handle_yt_error(e, current_user.username, "fetching search suggestions")


# --- Library & History Endpoints ---


@router.get("/library", response_model=LibraryResponse)
async def get_library(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_yt_auth(current_user)
    try:
        proxy_base = get_proxy_base(request)

        # YT playlists
        raw_yt = yt_service.get_client(current_user).get_library_playlists(limit=100)
        yt_playlists = [
            normalize_playlist(p, proxy_base, playlist_type="yt") for p in raw_yt
        ]

        # Flow playlists stored in the DB
        flow_db = db.query(Playlist).filter(Playlist.owner_id == current_user.id).all()
        flow_playlists = [
            PlaylistResponse(
                id=p.id,
                name=p.title,
                description=p.description or "",
                thumbnailUrl=p.thumbnail_url,
                trackCount=len(p.tracks),
                type="flow",
                isAlbum=False,
                ownerCode=current_user.user_code,
            )
            for p in flow_db
        ]

        return LibraryResponse(playlists=flow_playlists + yt_playlists)
    except Exception as e:
        _handle_yt_error(e, current_user.username, "fetching library")


@router.get("/history/yt", response_model=List[SongResponse])
async def get_yt_history(
    request: Request, current_user: User = Depends(get_current_user)
):
    _require_yt_auth(current_user)
    try:
        proxy_base = get_proxy_base(request)
        raw = yt_service.get_client(current_user).get_history()
        return [s for item in raw if (s := normalize_song(item, proxy_base))]
    except Exception as e:
        _handle_yt_error(e, current_user.username, "fetching history")


@router.post("/history", response_model=SongResponse)
async def record_play(
    song: SongResponse,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Save a played song to persistent history."""
    history_entry = PlayHistory(
        user_id=current_user.id,
        song_id=song.id,
        title=song.title,
        artist=song.artist,
        album=song.album,
        duration_ms=song.durationMs,
        thumbnail_url=song.thumbnailUrl,
        played_at=datetime.utcnow(),
    )
    db.add(history_entry)
    db.commit()
    db.refresh(history_entry)
    return song


@router.get("/history", response_model=HistoryResponse)
async def get_persistent_history(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    """Retrieve database-backed play history with date segmentation."""
    entries = (
        db.query(PlayHistory)
        .filter(PlayHistory.user_id == current_user.id)
        .order_by(PlayHistory.played_at.desc())
        .all()
    )

    from datetime import datetime as dt

    now = dt.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=now.weekday())
    month_start = today_start.replace(day=1)

    res = HistoryResponse()

    for e in entries:
        item = HistoryEntryResponse(
            id=e.song_id,
            title=e.title,
            artist=e.artist,
            album=e.album,
            durationMs=e.duration_ms,
            thumbnailUrl=e.thumbnail_url,
            playedAt=e.played_at,
        )

        if e.played_at >= today_start:
            res.today.append(item)
        elif e.played_at >= week_start:
            res.thisWeek.append(item)
        elif e.played_at >= month_start:
            res.thisMonth.append(item)
        else:
            month_key = e.played_at.strftime("%B %Y")
            if month_key not in res.byMonth:
                res.byMonth[month_key] = []
            res.byMonth[month_key].append(item)

    return res


# --- Browsing & Content Endpoints ---


@router.get("/playlists/{playlist_id}/tracks", response_model=List[SongResponse])
async def get_playlist_tracks(
    request: Request,
    response: Response,
    playlist_id: str,
    limit: int = 100,
    current_user: User = Depends(get_current_user),
):
    response.headers["Cache-Control"] = "public, max-age=600"
    try:
        proxy_base = get_proxy_base(request)
        actual_limit = None if limit <= 0 else limit
        data = yt_service.get_client(current_user).get_playlist(
            playlistId=playlist_id, limit=actual_limit
        )
        tracks = data.get("tracks") or []
        return [s for item in tracks if (s := normalize_song(item, proxy_base))]
    except Exception as e:
        _handle_yt_error(
            e, current_user.username, f"fetching tracks for playlist {playlist_id}"
        )


@router.get("/radio/{video_id}")
async def get_radio(
    request: Request,
    video_id: str,
    limit: int = 25,
    current_user: User = Depends(get_current_user),
):
    try:
        proxy_base = get_proxy_base(request)
        data = yt_service.get_client(current_user).get_watch_playlist(
            videoId=video_id, limit=limit
        )
        tracks = data.get("tracks") or []
        return [s for item in tracks if (s := normalize_song(item, proxy_base))]
    except Exception as e:
        _handle_yt_error(e, current_user.username, "fetching radio")


@router.get("/albums/{browse_id}", response_model=List[SongResponse])
async def get_album(
    request: Request,
    response: Response,
    browse_id: str,
    current_user: User = Depends(get_current_user),
):
    response.headers["Cache-Control"] = "public, max-age=600"
    try:
        proxy_base = get_proxy_base(request)
        data = yt_service.get_client(current_user).get_album(browseId=browse_id)

        # Album tracks often don't have their own thumbnails. Inherit from the album.
        album_thumbnails = data.get("thumbnails") or data.get("thumbnail") or []
        album_thumb_url = None
        if isinstance(album_thumbnails, dict):
            album_thumbnails = album_thumbnails.get("thumbnails", [])
        if (
            album_thumbnails
            and isinstance(album_thumbnails, list)
            and len(album_thumbnails) > 0
        ):
            album_thumb_url = (
                album_thumbnails[-1].get("url")
                if isinstance(album_thumbnails[-1], dict)
                else None
            )

        tracks = data.get("tracks") or []
        for track in tracks:
            # If the track doesn't have thumbnails, inject the album's thumbnail
            if (
                album_thumb_url
                and not track.get("thumbnails")
                and not track.get("thumbnail")
            ):
                track["thumbnails"] = [{"url": album_thumb_url}]

        return [s for item in tracks if (s := normalize_song(item, proxy_base))]
    except Exception as e:
        _handle_yt_error(e, current_user.username, f"fetching album {browse_id}")


@router.get("/artists/{channel_id}")
async def get_artist(channel_id: str, current_user: User = Depends(get_current_user)):
    try:
        return yt_service.get_client(current_user).get_artist(channelId=channel_id)
    except Exception as e:
        _handle_yt_error(e, current_user.username, f"fetching artist {channel_id}")


@router.get("/artists/{channel_id}/songs", response_model=List[SongResponse])
async def get_artist_songs(
    request: Request, channel_id: str, current_user: User = Depends(get_current_user)
):
    try:
        proxy_base = get_proxy_base(request)
        client = yt_service.get_client(current_user)
        artist_data = client.get_artist(channelId=channel_id)

        songs_data = artist_data.get("songs", {})
        results = songs_data.get("results", [])

        # If there's a "browseId" for "All songs", we might want to fetch that,
        # but for a quick overview, the first few are usually enough.
        # However, to be thorough:
        browse_id = songs_data.get("browseId")
        if browse_id:
            results = client.get_playlist(browse_id).get("tracks", [])

        return [s for item in results if (s := normalize_song(item, proxy_base))]
    except Exception as e:
        _handle_yt_error(
            e, current_user.username, f"fetching artist songs for {channel_id}"
        )


@router.get("/songs/lyrics/{video_id}")
async def get_lyrics(video_id: str, current_user: User = Depends(get_current_user)):
    try:
        client = yt_service.get_client(current_user)
        watch = client.get_watch_playlist(videoId=video_id)
        lyrics_id = watch.get("lyrics")
        if not lyrics_id:
            return {"lyrics": None}
        return client.get_lyrics(lyrics_id)
    except Exception as e:
        _handle_yt_error(e, current_user.username, f"fetching lyrics for {video_id}")


@router.get("/songs/batch", response_model=List[SongResponse])
async def get_songs_batch(
    request: Request, ids: str, current_user: User = Depends(get_current_user)
):
    """Fetch multiple songs by a comma-separated list of IDs."""
    if not ids.strip():
        return []
    video_ids = [vid.strip() for vid in ids.split(",") if vid.strip()]
    logger.debug(
        f"Batch fetching {len(video_ids)} songs for user {current_user.username}"
    )

    try:
        proxy_base = get_proxy_base(request)
        client = yt_service.get_client(current_user)

        from anyio.to_thread import run_sync

        async def fetch_song(vid):
            try:
                # get_song returns a dict with basic info
                data = await run_sync(client.get_song, vid)
                if data and isinstance(data, dict) and "videoDetails" in data:
                    # Map YT Music videoDetails to our SongResponse
                    details = data["videoDetails"]
                    raw_thumb = (
                        details.get("thumbnail", {})
                        .get("thumbnails", [{}])[-1]
                        .get("url")
                    )
                    return SongResponse(
                        id=details["videoId"],
                        title=details["title"],
                        artist=details["author"],
                        album="",  # Not always in videoDetails
                        durationMs=int(details["lengthSeconds"]) * 1000,
                        thumbnailUrl=fix_thumbnail_url(raw_thumb, proxy_base),
                    )
            except Exception as e:
                logger.warning(f"Failed to fetch batch song {vid}: {e}")
            return None

        # Fetch in parallel
        tasks = [fetch_song(vid) for vid in video_ids]
        results = await asyncio.gather(*tasks)
        return [r for r in results if r is not None]
    except Exception as e:
        _handle_yt_error(e, current_user.username, "batch fetching songs")


# --- Playlist Management Endpoints ---


@router.post("/playlists")
async def create_playlist(
    req: CreatePlaylistRequest, current_user: User = Depends(get_current_user)
):
    _require_yt_auth(current_user)
    try:
        res = yt_service.get_client(current_user).create_playlist(
            title=req.title,
            description=req.description,
            privacy_status=req.privacy_status,
            video_ids=req.video_ids,
            source_playlist=req.source_playlist,
        )
        return {"id": res}
    except Exception as e:
        _handle_yt_error(e, current_user.username, "creating playlist")


@router.patch("/playlists/{playlist_id}")
async def edit_playlist(
    playlist_id: str,
    req: EditPlaylistRequest,
    current_user: User = Depends(get_current_user),
):
    _require_yt_auth(current_user)
    try:
        res = yt_service.get_client(current_user).edit_playlist(
            playlistId=playlist_id,
            title=req.title,
            description=req.description,
            privacyStatus=req.privacyStatus,
            moveItem=req.moveItem,
            addPlaylistId=req.addPlaylistId,
            addToTop=req.addToTop,
        )
        return {"status": res}
    except Exception as e:
        _handle_yt_error(e, current_user.username, "editing playlist")


@router.delete("/playlists/{playlist_id}")
async def delete_playlist(
    playlist_id: str, current_user: User = Depends(get_current_user)
):
    _require_yt_auth(current_user)
    try:
        res = yt_service.get_client(current_user).delete_playlist(
            playlistId=playlist_id
        )
        return {"status": res}
    except Exception as e:
        _handle_yt_error(e, current_user.username, "deleting playlist")


@router.post("/playlists/{playlist_id}/items")
async def add_playlist_items(
    playlist_id: str,
    req: AddPlaylistItemsRequest,
    current_user: User = Depends(get_current_user),
):
    _require_yt_auth(current_user)
    try:
        res = yt_service.get_client(current_user).add_playlist_items(
            playlistId=playlist_id,
            videoIds=req.videoIds,
            source_playlist=req.source_playlist,
            duplicates=req.duplicates,
        )
        return {"status": res}
    except Exception as e:
        _handle_yt_error(e, current_user.username, "adding items to playlist")


@router.delete("/playlists/{playlist_id}/items")
async def remove_playlist_items(
    playlist_id: str,
    req: RemovePlaylistItemsRequest,
    current_user: User = Depends(get_current_user),
):
    _require_yt_auth(current_user)
    try:
        res = yt_service.get_client(current_user).remove_playlist_items(
            playlistId=playlist_id, videos=req.videos
        )
        return {"status": res}
    except Exception as e:
        _handle_yt_error(e, current_user.username, "removing items from playlist")


# --- Flow Playlist CRUD ---


import uuid as _uuid


@router.post("/flow/playlists", response_model=PlaylistResponse, status_code=201)
async def create_flow_playlist(
    req: FlowPlaylistCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    new_playlist = Playlist(
        id=str(_uuid.uuid4()),
        title=req.title,
        description=req.description,
        is_public=req.is_public,
        owner_id=current_user.id,
        type="flow",
    )
    db.add(new_playlist)
    db.commit()
    db.refresh(new_playlist)
    return PlaylistResponse(
        id=new_playlist.id,
        name=new_playlist.title,
        description=new_playlist.description or "",
        thumbnailUrl=new_playlist.thumbnail_url,
        trackCount=0,
        type="flow",
        isAlbum=False,
        ownerCode=current_user.user_code,
    )


@router.patch("/flow/playlists/{playlist_id}", response_model=PlaylistResponse)
async def update_flow_playlist(
    playlist_id: str,
    req: FlowPlaylistUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    playlist = (
        db.query(Playlist)
        .filter(Playlist.id == playlist_id, Playlist.owner_id == current_user.id)
        .first()
    )
    if not playlist:
        raise HTTPException(404, "Playlist not found")
    if req.title is not None:
        playlist.title = req.title
    if req.description is not None:
        playlist.description = req.description
    if req.is_public is not None:
        playlist.is_public = req.is_public
    db.commit()
    db.refresh(playlist)
    return PlaylistResponse(
        id=playlist.id,
        name=playlist.title,
        description=playlist.description or "",
        thumbnailUrl=playlist.thumbnail_url,
        trackCount=len(playlist.tracks),
        type="flow",
        isAlbum=False,
        ownerCode=current_user.user_code,
    )


@router.delete("/flow/playlists/{playlist_id}", status_code=204)
async def delete_flow_playlist(
    playlist_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    playlist = (
        db.query(Playlist)
        .filter(Playlist.id == playlist_id, Playlist.owner_id == current_user.id)
        .first()
    )
    if not playlist:
        raise HTTPException(404, "Playlist not found")
    db.delete(playlist)
    db.commit()


@router.post("/flow/playlists/{playlist_id}/tracks", status_code=201)
async def add_track_to_flow_playlist(
    playlist_id: str,
    req: FlowPlaylistAddTrackRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    playlist = db.query(Playlist).filter(Playlist.id == playlist_id).first()
    if not playlist:
        raise HTTPException(404, "Playlist not found")
    # Allow owner and collaborators to add tracks
    is_owner = playlist.owner_id == current_user.id
    is_collab = (
        db.query(PlaylistCollaborator)
        .filter(
            PlaylistCollaborator.playlist_id == playlist_id,
            PlaylistCollaborator.user_id == current_user.id,
        )
        .first()
        is not None
    )
    if not is_owner and not is_collab:
        raise HTTPException(403, "Not authorized")

    max_idx = (
        db.query(PlaylistTrack).filter(PlaylistTrack.playlist_id == playlist_id).count()
    )
    track = PlaylistTrack(
        playlist_id=playlist_id,
        song_data=json.dumps(req.song_data),
        sort_index=max_idx,
    )
    db.add(track)
    db.commit()
    db.refresh(track)
    return {"id": track.id, "sort_index": track.sort_index}


@router.delete("/flow/playlists/{playlist_id}/tracks/{track_id}", status_code=204)
async def remove_track_from_flow_playlist(
    playlist_id: str,
    track_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    playlist = db.query(Playlist).filter(Playlist.id == playlist_id).first()
    if not playlist:
        raise HTTPException(404, "Playlist not found")
    is_owner = playlist.owner_id == current_user.id
    is_collab = (
        db.query(PlaylistCollaborator)
        .filter(
            PlaylistCollaborator.playlist_id == playlist_id,
            PlaylistCollaborator.user_id == current_user.id,
        )
        .first()
        is not None
    )
    if not is_owner and not is_collab:
        raise HTTPException(403, "Not authorized")

    track = (
        db.query(PlaylistTrack)
        .filter(PlaylistTrack.id == track_id, PlaylistTrack.playlist_id == playlist_id)
        .first()
    )
    if not track:
        raise HTTPException(404, "Track not found")
    db.delete(track)
    db.commit()


@router.post("/flow/playlists/{playlist_id}/collaborators", status_code=201)
async def add_collaborator(
    playlist_id: str,
    req: FlowCollaboratorRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    playlist = (
        db.query(Playlist)
        .filter(Playlist.id == playlist_id, Playlist.owner_id == current_user.id)
        .first()
    )
    if not playlist:
        raise HTTPException(404, "Playlist not found or not owner")

    target = db.query(User).filter(User.user_code == req.user_code).first()
    if not target:
        raise HTTPException(404, f"No user with code '{req.user_code}'")

    existing = (
        db.query(PlaylistCollaborator)
        .filter(
            PlaylistCollaborator.playlist_id == playlist_id,
            PlaylistCollaborator.user_id == target.id,
        )
        .first()
    )
    if existing:
        return {"status": "already_collaborator"}

    collab = PlaylistCollaborator(
        playlist_id=playlist_id, user_id=target.id, role="editor"
    )
    db.add(collab)
    db.commit()
    return {"status": "added", "user_code": req.user_code}


@router.delete(
    "/flow/playlists/{playlist_id}/collaborators/{user_code}", status_code=204
)
async def remove_collaborator(
    playlist_id: str,
    user_code: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    playlist = (
        db.query(Playlist)
        .filter(Playlist.id == playlist_id, Playlist.owner_id == current_user.id)
        .first()
    )
    if not playlist:
        raise HTTPException(404, "Playlist not found or not owner")

    target = db.query(User).filter(User.user_code == user_code).first()
    if not target:
        raise HTTPException(404, f"No user with code '{user_code}'")

    collab = (
        db.query(PlaylistCollaborator)
        .filter(
            PlaylistCollaborator.playlist_id == playlist_id,
            PlaylistCollaborator.user_id == target.id,
        )
        .first()
    )
    if not collab:
        raise HTTPException(404, "Collaborator not found")
    db.delete(collab)
    db.commit()


# --- YT Music Connection Management (Per User) ---


@router.get("/yt-status")
async def yt_status(current_user: User = Depends(get_current_user)):
    return {"connected": bool(current_user.yt_auth_json)}


@router.post("/yt-auth")
async def setup_yt_auth(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    logger.info(f"YT auth setup attempt for user: {current_user.username}")
    body = (await request.body()).decode("utf-8").strip()
    if not body:
        logger.warning(f"YT auth failed for {current_user.username}: Empty body")
        raise HTTPException(400, "Body is empty")

    if settings.DEBUG:
        logger.debug(f"Processing YT auth headers for {current_user.username}")

    headers_raw = curl_to_headers(body) if body.lstrip().startswith("curl") else body

    if "cookie" not in headers_raw.lower():
        logger.warning(
            f"YT auth failed for {current_user.username}: Missing cookie header"
        )
        raise HTTPException(
            400,
            "No cookie header found — make sure to include the full headers or cURL command",
        )

    try:
        # We use a temporary file to let ytmusicapi parse and validate the headers
        with tempfile.NamedTemporaryFile(
            mode="w+", suffix=".json", delete=False
        ) as tmp:
            tmp_path = tmp.name

        try:
            ytmusicapi.setup(tmp_path, headers_raw=headers_raw)
            with open(tmp_path, "r") as f:
                auth_data = json.load(f)

            # Update user in DB
            current_user.yt_auth_json = json.dumps(auth_data)
            db.add(current_user)
            db.commit()

            yt_service.clear_cache(current_user.id)
            logger.info(
                f"YT auth connected successfully for user: {current_user.username}"
            )
            return {"status": "ok", "message": "YouTube Music connected successfully"}
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    except Exception as e:
        logger.error(f"YT auth setup failed for {current_user.username}: {e}")
        raise HTTPException(400, f"Auth setup failed: {e}")


@router.post("/yt-auth/cookies")
async def setup_yt_auth_cookies(
    payload: YTCookiesPayload,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    logger.info(f"YT auth cookie setup attempt for user: {current_user.username}")
    cookie_str = "; ".join(f"{k}={v}" for k, v in payload.cookies.items())
    headers_raw = f"Cookie: {cookie_str}\nX-Goog-AuthUser: 0\n"
    try:
        with tempfile.NamedTemporaryFile(
            mode="w+", suffix=".json", delete=False
        ) as tmp:
            tmp_path = tmp.name
        try:
            ytmusicapi.setup(tmp_path, headers_raw=headers_raw)
            with open(tmp_path, "r") as f:
                auth_data = json.load(f)
            current_user.yt_auth_json = json.dumps(auth_data)
            db.add(current_user)
            db.commit()
            yt_service.clear_cache(current_user.id)
            logger.info(
                f"YT auth cookies connected successfully for user: {current_user.username}"
            )
            return {"status": "ok", "message": "YouTube Music connected successfully"}
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
    except Exception as e:
        logger.error(f"YT auth cookie setup failed for {current_user.username}: {e}")
        raise HTTPException(400, f"Auth setup failed: {e}")


@router.delete("/yt-auth")
async def yt_logout(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    logger.info(f"Disconnecting YT auth for user: {current_user.username}")
    current_user.yt_auth_json = None
    db.add(current_user)
    db.commit()
    yt_service.clear_cache(current_user.id)
    logger.info(f"YT auth disconnected for user: {current_user.username}")
    return {"status": "ok", "message": "YouTube Music disconnected"}


@router.post("/yt-auth/oauth/init", response_model=YTOAuthResponse)
async def init_yt_oauth(current_user: User = Depends(get_current_user)):
    """Initialize the YT Music OAuth flow on the server."""
    logger.info(f"Initializing YT OAuth for user: {current_user.username}")
    try:
        code_dict = yt_service.init_oauth()
        # code_dict contains: device_code, user_code, verification_url, expires_in, interval

        # Store for verification later
        yt_service.pending_oauth[code_dict["device_code"]] = {
            "user_id": current_user.id,
            "expiry": time.time() + code_dict["expires_in"],
        }

        return YTOAuthResponse(**code_dict)
    except Exception as e:
        logger.error(f"Failed to init YT OAuth for {current_user.username}: {e}")
        raise HTTPException(500, f"OAuth initialization failed: {e}")


@router.get("/yt-auth/oauth/check/{device_code}", response_model=YTOAuthStatus)
async def check_yt_oauth(
    device_code: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Check if the user has completed the OAuth flow."""
    pending = yt_service.pending_oauth.get(device_code)
    if not pending or pending["user_id"] != current_user.id:
        return YTOAuthStatus(
            status="expired", message="OAuth session not found or expired"
        )

    if time.time() > pending["expiry"]:
        del yt_service.pending_oauth[device_code]
        return YTOAuthStatus(status="expired", message="OAuth session expired")

    try:
        token_data = yt_service.finish_oauth(device_code)
        if token_data:
            # Success!
            current_user.yt_auth_json = json.dumps(token_data)
            db.add(current_user)
            db.commit()

            del yt_service.pending_oauth[device_code]
            yt_service.clear_cache(current_user.id)

            logger.info(f"YT OAuth completed successfully for {current_user.username}")
            return YTOAuthStatus(
                status="success", message="YouTube Music connected successfully"
            )
        else:
            return YTOAuthStatus(
                status="pending", message="Waiting for authorization..."
            )

    except Exception as e:
        logger.error(f"Error checking YT OAuth for {current_user.username}: {e}")
        return YTOAuthStatus(status="declined", message=f"Authorization failed: {e}")


# --- Streaming & Proxy ---


@router.get("/prefetch/{video_id}")
async def prefetch_audio(
    video_id: str,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
):
    """
    Proactively trigger extraction for a video_id to warm up the cache.
    """
    logger.info(f"Prefetch request for {video_id}")
    # Run extraction in the background
    background_tasks.add_task(extract_audio_url, video_id, user=current_user)
    return {"status": "ok", "video_id": video_id}


def track_interaction_background(user_id: int, video_id: str):
    """Background task to track interaction with a new DB session."""
    from .database import SessionLocal

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if user:
            yt_service.track_interaction(db, user, video_id)
    except Exception as e:
        logger.error(f"Background interaction tracking failed: {e}")
    finally:
        db.close()


@router.get("/stream/{video_id}")
async def stream_audio(
    video_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
):
    logger.info(f"Streaming request for {video_id} from {request.client.host}")

    # Track interaction in background using a fresh session
    background_tasks.add_task(track_interaction_background, current_user.id, video_id)

    try:
        audio_url = await extract_audio_url(video_id, user=current_user)
    except Exception as e:
        logger.error(f"Extraction failed for {video_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Extraction failed: {e}",
        )

    upstream_headers = {}
    if "range" in request.headers:
        upstream_headers["range"] = request.headers["range"]
        logger.info(f"Range request: {request.headers['range']} for {video_id}")

    # We'll use a standard Chrome User-Agent to match the impersonation used by yt-dlp
    upstream_headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            "Referer": "https://www.youtube.com/",
            "Connection": "keep-alive",
        }
    )

    client = get_shared_client()
    try:
        # We use a stream context to ensure the connection is closed
        request_to_upstream = httpx.Request("GET", audio_url, headers=upstream_headers)
        upstream = await client.send(request_to_upstream, stream=True)

        logger.info(
            f"Upstream response for {video_id}: status={upstream.status_code} "
            f"content-type={upstream.headers.get('content-type')} "
            f"content-length={upstream.headers.get('content-length')} "
            f"range={upstream.headers.get('content-range')}"
        )

        if upstream.status_code >= 400:
            logger.error(f"Upstream returned {upstream.status_code} for {video_id}")
            # If forbidden or not found, try to clear cache so next request triggers re-extraction
            if upstream.status_code in (403, 410):
                from .services import _url_cache

                if video_id in _url_cache:
                    del _url_cache[video_id]

        passthrough = {
            k: v
            for k, v in upstream.headers.items()
            if k.lower()
            in (
                "content-type",
                "content-range",
                "accept-ranges",
                "etag",
                "last-modified",
            )
        }
        # Only pass content-length if we are serving a range or if we are sure it won't confuse the client
        if "content-length" in upstream.headers and "range" in request.headers:
            passthrough["content-length"] = upstream.headers["content-length"]

        if "accept-ranges" not in passthrough:
            passthrough["accept-ranges"] = "bytes"

        # Determine media type more robustly
        content_type = upstream.headers.get("content-type")
        if not content_type or content_type == "application/octet-stream":
            if ".googlevideo.com" in audio_url:
                if "mime=audio%2Fmp4" in audio_url or "mime=audio/mp4" in audio_url:
                    content_type = "audio/mp4"
                elif "mime=audio%2Fwebm" in audio_url or "mime=audio/webm" in audio_url:
                    content_type = "audio/webm"
                else:
                    content_type = "audio/webm"
            else:
                content_type = "audio/mpeg"

        logger.info(
            f"Serving stream for {video_id} as {content_type} with headers: {passthrough}"
        )

        async def _iter():
            logger.info(f"Starting to yield chunks for {video_id}")
            total_sent = 0
            try:
                # Use a smaller chunk size (64KB) to start playback faster
                async for chunk in upstream.aiter_bytes(65536):
                    if total_sent == 0:
                        logger.info(f"First chunk yielded for {video_id}")
                    total_sent += len(chunk)
                    yield chunk
                logger.info(
                    f"Finished streaming {video_id}: sent {total_sent} bytes total"
                )
            except Exception as e:
                # Client probably disconnected
                logger.warning(
                    f"Streaming interrupted for {video_id} after {total_sent} bytes: {e}"
                )
            finally:
                await upstream.aclose()

        return StreamingResponse(
            _iter(),
            status_code=upstream.status_code,
            headers=passthrough,
            media_type=content_type,
        )
    except Exception as e:
        import traceback

        logger.error(
            f"Upstream connection failed for {video_id}: {e}\n{traceback.format_exc()}"
        )
        raise HTTPException(status_code=502, detail=f"Upstream error: {e}")


def _rotate_image_cache():
    """Maintain the image cache size under the configured limit (LRU)."""
    try:
        cache_dir = pathlib.Path(settings.IMAGE_CACHE_DIR)
        max_size_bytes = settings.MAX_IMAGE_CACHE_SIZE_MB * 1024 * 1024

        # Get all files with their size and last access time
        files = []
        total_size = 0
        for f in cache_dir.glob("*"):
            if f.is_file():
                stat = f.stat()
                files.append({"path": f, "size": stat.st_size, "atime": stat.st_atime})
                total_size += stat.st_size

        if total_size <= max_size_bytes:
            return

        # Sort by access time (oldest first)
        files.sort(key=lambda x: x["atime"])

        for f in files:
            if total_size <= max_size_bytes * 0.9:  # Give some breathing room
                break
            try:
                f["path"].unlink()
                total_size -= f["size"]
                logger.debug(f"Cache rotation: Deleted {f['path'].name}")
            except Exception as e:
                logger.warning(f"Failed to delete {f['path']}: {e}")

    except Exception as e:
        logger.error(f"Cache rotation failed: {e}")


@router.get("/proxy-image")
async def proxy_image(url: str, background_tasks: BackgroundTasks):
    try:
        decoded_url = urllib.parse.unquote(url)
        if not decoded_url.startswith("http"):
            raise HTTPException(400, "Invalid image URL")

        # 1. Generate Cache Key
        url_hash = hashlib.sha256(decoded_url.encode()).hexdigest()
        cache_path = pathlib.Path(settings.IMAGE_CACHE_DIR) / url_hash
        meta_path = pathlib.Path(settings.IMAGE_CACHE_DIR) / f"{url_hash}.meta"

        # 2. Check Cache
        if cache_path.exists() and meta_path.exists():
            try:
                content = cache_path.read_bytes()
                media_type = meta_path.read_text().strip()
                logger.debug(f"Image proxy cache hit: {url_hash}")
                # Update atime for LRU
                cache_path.touch()
                return Response(
                    content=content,
                    media_type=media_type,
                    headers={"Cache-Control": "public, max-age=31536000"},
                )
            except Exception as e:
                logger.warning(f"Cache read failed for {url_hash}: {e}")

        # 3. Cache Miss - Fetch from Upstream
        client = get_shared_client()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        }
        resp = await client.get(decoded_url, headers=headers, timeout=10.0)

        if resp.status_code != 200:
            logger.warning(
                f"Image proxy fetch failed for {decoded_url}: {resp.status_code}"
            )
            return Response(status_code=resp.status_code)

        content = resp.content
        media_type = resp.headers.get("Content-Type", "image/jpeg")

        # 4. Save to Cache
        try:
            cache_path.write_bytes(content)
            meta_path.write_text(media_type)
            # Trigger rotation in background
            background_tasks.add_task(_rotate_image_cache)
        except Exception as e:
            logger.error(f"Failed to write image cache for {url_hash}: {e}")

        return Response(
            content=content,
            media_type=media_type,
            headers={"Cache-Control": "public, max-age=31536000"},
        )
    except Exception as e:
        logger.error(f"Image proxy exception for {url}: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Server-side browser control  (admin — any authenticated user)
# Lets the app drive a headless Chromium running on the server so that the
# resulting cookies are issued to the server's IP and work with yt-dlp.
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/admin/browser/start", response_model=BrowserFrameResponse)
async def browser_start(current_user: User = Depends(get_current_user)):
    """Start (or resume) a headless Chromium at the YouTube/Google login page."""
    logger.info(f"Browser session start requested by {current_user.username}")
    try:
        screenshot = await browser_session.start(
            "https://accounts.google.com/ServiceLogin?service=youtube"
        )
        return BrowserFrameResponse(screenshot=screenshot, is_active=True)
    except Exception as e:
        logger.error(f"Browser start failed: {e}")
        raise HTTPException(500, f"Browser start failed: {e}")


@router.get("/admin/browser/frame", response_model=BrowserFrameResponse)
async def browser_frame(current_user: User = Depends(get_current_user)):
    """Return a fresh screenshot of the current browser page."""
    if not browser_session.is_active:
        return BrowserFrameResponse(screenshot="", is_active=False)
    try:
        screenshot = await browser_session.screenshot()
        return BrowserFrameResponse(screenshot=screenshot, is_active=True)
    except Exception as e:
        logger.error(f"Browser screenshot failed: {e}")
        raise HTTPException(500, f"Screenshot failed: {e}")


@router.post("/admin/browser/tap", response_model=BrowserFrameResponse)
async def browser_tap(
    req: BrowserTapRequest,
    current_user: User = Depends(get_current_user),
):
    """Click at fractional coordinates (0–1) in the browser viewport."""
    try:
        screenshot = await browser_session.tap(req.x, req.y)
        return BrowserFrameResponse(screenshot=screenshot, is_active=True)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/admin/browser/type", response_model=BrowserFrameResponse)
async def browser_type(
    req: BrowserTypeRequest,
    current_user: User = Depends(get_current_user),
):
    """Type text into the focused browser element."""
    try:
        screenshot = await browser_session.type_text(req.text)
        return BrowserFrameResponse(screenshot=screenshot, is_active=True)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/admin/browser/key", response_model=BrowserFrameResponse)
async def browser_key(
    req: BrowserKeyRequest,
    current_user: User = Depends(get_current_user),
):
    """Send a special key (Enter, Backspace, Tab, …) to the browser."""
    try:
        screenshot = await browser_session.key_press(req.key)
        return BrowserFrameResponse(screenshot=screenshot, is_active=True)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/admin/browser/save")
async def browser_save(current_user: User = Depends(get_current_user)):
    """Save cookies from the current browser session to the server's cookies.txt."""
    try:
        count = await browser_session.save_cookies(settings.COOKIES_FILE_PATH)
        logger.info(f"Browser cookies saved by {current_user.username}: {count} cookies")
        return {"status": "ok", "message": f"Saved {count} cookies", "count": count}
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.error(f"Browser save failed: {e}")
        raise HTTPException(500, f"Save failed: {e}")


@router.delete("/admin/browser/stop")
async def browser_stop(current_user: User = Depends(get_current_user)):
    """Stop the browser session without saving cookies."""
    await browser_session.stop()
    return {"status": "ok", "message": "Browser stopped"}
