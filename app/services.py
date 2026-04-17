import asyncio
import hashlib
import json
import logging
import os
import time
import traceback
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import anyio
import diskcache
import yt_dlp
import ytmusicapi
from anyio.to_thread import run_sync
from jose import JWTError, jwt

# from passlib.context import CryptContext
from sqlalchemy.orm import Session

from .config import settings
from .models import ArtistResponse, HomeResponse, SongResponse, User, UserSongInteraction, UserRecommendation
from .utils import (
    is_artist_item,
    normalize_artist,
    normalize_playlist,
    normalize_song,
    normalize_album_as_song,
    write_cookie_file,
)

logger = logging.getLogger("flow.services")

# ── API Cache ─────────────────────────────────────────────────────────────────
# Robust persistent cache for expensive API responses
_api_cache = diskcache.Cache(os.path.join("./data", "api_cache"))

# Password hashing — bcrypt disabled for now (passlib/bcrypt version mismatch)
# pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


class AuthService:
    @staticmethod
    def verify_password(plain_password, hashed_password):
        # return pwd_context.verify(plain_password, hashed_password)
        return plain_password == hashed_password

    @staticmethod
    def get_password_hash(password):
        # return pwd_context.hash(password)
        return password

    @staticmethod
    def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
        to_encode = data.copy()
        if expires_delta:
            expire = datetime.utcnow() + expires_delta
        else:
            expire = datetime.utcnow() + timedelta(minutes=15)
        to_encode.update({"exp": expire})
        encoded_jwt = jwt.encode(
            to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM
        )
        return encoded_jwt

    @staticmethod
    def generate_user_code(username: str, db: Session) -> str:
        """Generate a unique user code like username#1234."""
        import random
        
        # Clean username (remove spaces, etc if needed, but let's keep it simple for now)
        base = username.replace(" ", "").lower()
        
        for _ in range(10):  # Try 10 times to find a unique code
            code = f"{base}#{random.randint(1000, 9999)}"
            # Check DB for uniqueness
            existing = db.query(User).filter(User.user_code == code).first()
            if not existing:
                return code
        
        # Fallback to a longer code if collisions persist
        return f"{base}#{random.randint(100000, 999999)}"


class YTMusicService:
    def __init__(self):
        # We no longer rely on a global settings.AUTH_FILE_PATH for all requests
        self.home_cache = {}
        self.home_cache_ttl = 300
        self._shelf_map = [
            (["quick pick", "top pick", "start radio", "picks", "suggested"], "quickPicks"),
            (["listen again", "listening again", "continue", "recent", "replay"], "listeningAgain"),
            (["fresh find", "new release", "latest", "just out", "new arrival"], "freshFinds"),
            (
                [
                    "picked for you",
                    "for you",
                    "mixed",
                    "your",
                    "personalized",
                    "discover",
                    "mix",
                ],
                "pickedForYou",
            ),
            (["forgotten", "throwback", "rediscover", "missed"], "forgottenFavorites"),
            (["album", "mpreb"], "albumsForYou"),
            (
                ["mood", "genre", "vibe", "energy", "workout", "focus", "relax"],
                "moodsAndGenres",
            ),
            (["top chart", "trending", "popular", "global", "hits"], "trending"),
            (["similar to", "related to", "based on", "recommended", "fans", "might also like"], "similarTo"),
            (["artist spotlight", "from your fav"], "artistSpotlight"),
            (["video"], "musicVideos"),
        ]

    def get_client(self, user: Optional[User] = None):
        if user and user.yt_auth_json:
            try:
                from ytmusicapi.helpers import get_authorization, sapisid_from_cookie

                logger.debug(f"Creating authenticated client for user: {user.username}")
                auth_data = json.loads(user.yt_auth_json)

                # Map common lowercase keys to their expected Title-Case versions
                # ytmusicapi 1.11.5 determine_auth_type is case-sensitive for 'Authorization' 
                # and 'Cookie' when checking for BROWSER type.
                normalization_map = {
                    "cookie": "Cookie",
                    "user-agent": "User-Agent",
                    "x-goog-authuser": "X-Goog-AuthUser",
                    "authorization": "Authorization",
                    "origin": "Origin",
                }

                normalized_auth = {}
                for k, v in auth_data.items():
                    norm_key = normalization_map.get(k.lower(), k)
                    # If not in map and looks like a header, Title-Case it
                    if k.lower() not in normalization_map and "-" in k:
                        norm_key = "-".join([p.capitalize() for p in k.split("-")])
                    normalized_auth[norm_key] = v

                # Ensure Authorization header is present (Crucial for ytmusicapi 1.11.5)
                # It uses this to distinguish BROWSER type from OAUTH_CUSTOM_CLIENT.
                if "Cookie" in normalized_auth and "Authorization" not in normalized_auth:
                    try:
                        cookie_val = normalized_auth["Cookie"]
                        sapisid = sapisid_from_cookie(cookie_val)
                        origin = normalized_auth.get("Origin", "https://music.youtube.com")
                        normalized_auth["Authorization"] = get_authorization(
                            sapisid + " " + origin
                        )
                        logger.debug(f"Calculated missing Authorization header for {user.username}")
                    except Exception as e:
                        logger.warning(f"Failed to calculate Authorization header: {e}")

                # Pass dictionary directly to YTMusic (supported in 1.11.5)
                # This avoids tempfile overhead and extension-based type detection issues.
                client = ytmusicapi.YTMusic(auth=normalized_auth)
                logger.debug(
                    f"Client initialized for {user.username} (type={getattr(client, 'auth_type', 'unknown')})"
                )
                return client

            except Exception as e:
                logger.error(
                    f"Failed to initialize YT auth for user {user.username}: {e}\n{traceback.format_exc()}"
                )
                # Fallback to global/unauthenticated if user auth fails

        # Check if a global auth file still exists (for backward compatibility or shared dev)
        if os.path.exists(settings.AUTH_FILE_PATH):
            logger.debug(
                f"Creating authenticated client from global file: {settings.AUTH_FILE_PATH}"
            )
            try:
                return ytmusicapi.YTMusic(settings.AUTH_FILE_PATH)
            except Exception as e:
                logger.error(f"Failed to initialize global YT auth: {e}")

        logger.debug("Creating unauthenticated YTMusic client")
        return ytmusicapi.YTMusic()

    def _classify_shelf(self, title: str) -> Optional[str]:
        t = title.lower()
        for keywords, section in self._shelf_map:
            if any(k in t for k in keywords):
                return section
        return None

    def _get_trending_songs(
        self, ytm, proxy_base: Optional[str] = None
    ) -> List[SongResponse]:
        try:
            charts = ytm.get_charts(country="ZZ")
            songs_chart = charts.get("songs") or {}
            items = songs_chart.get("items") or []
            if not items:
                trending_chart = charts.get("trending") or {}
                items = trending_chart.get("items") or []
            return [s for item in items[:20] if (s := normalize_song(item, proxy_base))]
        except Exception:
            return []

    def track_interaction(
        self,
        db: Session,
        user: User,
        song_id: str,
        genres: Optional[List[str]] = None,
    ):
        interaction = (
            db.query(UserSongInteraction)
            .filter(
                UserSongInteraction.user_id == user.id,
                UserSongInteraction.song_id == song_id,
            )
            .first()
        )

        if not interaction:
            interaction = UserSongInteraction(
                user_id=user.id,
                song_id=song_id,
                play_count=1,
                genre_tags=json.dumps(genres) if genres else None,
            )
            db.add(interaction)
        else:
            interaction.play_count += 1
            interaction.last_played_at = datetime.utcnow()
            if genres:
                existing_genres = (
                    json.loads(interaction.genre_tags) if interaction.genre_tags else []
                )
                updated_genres = list(set(existing_genres + genres))
                interaction.genre_tags = json.dumps(updated_genres)

        db.commit()

    def generate_recommendations(
        self,
        db: Session,
        user: User,
        ytm,
        proxy_base: Optional[str] = None,
    ) -> List[SongResponse]:
        """
        Sophisticated recommendation engine:
        1. Seeds from local interactions (most played, most recent).
        2. Seeds from top genres.
        3. Seeds from liked artists.
        4. Mixes related tracks and trends.
        """
        try:
            import random

            recommendations = []
            seen_ids = set()

            # 1. Identify Seeds
            # Get top 3 most played
            top_played = (
                db.query(UserSongInteraction)
                .filter(UserSongInteraction.user_id == user.id)
                .order_by(UserSongInteraction.play_count.desc())
                .limit(3)
                .all()
            )
            # Get 3 most recent
            recent_played = (
                db.query(UserSongInteraction)
                .filter(UserSongInteraction.user_id == user.id)
                .order_by(UserSongInteraction.last_played_at.desc())
                .limit(3)
                .all()
            )

            seed_song_ids = list(set([i.song_id for i in top_played + recent_played]))

            # 2. Fetch related for seeds
            for video_id in seed_song_ids[:5]:  # Limit seeds to avoid latency
                try:
                    radio = ytm.get_watch_playlist(videoId=video_id, limit=10)
                    tracks = radio.get("tracks", [])
                    for t in tracks:
                        if t.get("videoId") and t["videoId"] not in seen_ids:
                            song = normalize_song(t, proxy_base)
                            if song:
                                recommendations.append(song)
                                seen_ids.add(song.id)
                except Exception as e:
                    logger.warning(f"RecSys: Radio for {video_id} failed: {e}")

            # 3. Seeds from Library Artists
            try:
                liked_artists = ytm.get_library_artists(limit=10)
                if liked_artists:
                    random_artists = random.sample(
                        liked_artists, min(len(liked_artists), 2)
                    )
                    for artist in random_artists:
                        artist_data = ytm.get_artist(artist["browseId"])
                        songs = artist_data.get("songs", {}).get("results", [])
                        for s in songs[:5]:
                            if s.get("videoId") and s["videoId"] not in seen_ids:
                                song = normalize_song(s, proxy_base)
                                if song:
                                    recommendations.append(song)
                                    seen_ids.add(song.id)
            except Exception as e:
                logger.warning(f"RecSys: Artist seeds failed: {e}")

            # 4. Mix in Trends (Explore)
            try:
                explore = ytm.get_explore()
                new_releases = explore.get("new_releases", [])
                for item in new_releases[:5]:
                    song = normalize_album_as_song(item, proxy_base)
                    if song and song.id not in seen_ids:
                        recommendations.append(song)
                        seen_ids.add(song.id)
            except Exception as e:
                logger.warning(f"RecSys: Explore seeds failed: {e}")

            random.shuffle(recommendations)
            final_recs = recommendations[:40]

            # 5. Persist to DB
            # Clear old recs for this user
            db.query(UserRecommendation).filter(
                UserRecommendation.user_id == user.id
            ).delete()

            for i, song in enumerate(final_recs):
                rec = UserRecommendation(
                    user_id=user.id,
                    song_id=song.id,
                    data=song.model_dump_json(),
                    score=1.0 / (i + 1),  # Simple score based on shuffled order
                    updated_at=datetime.utcnow(),
                )
                db.add(rec)
            db.commit()

            return final_recs
        except Exception as e:
            logger.error(f"RecSys: Systemic failure: {e}\n{traceback.format_exc()}")
            return []

    def _get_fresh_picks_local(
        self, db: Session, user: User, proxy_base: Optional[str] = None
    ) -> List[SongResponse]:
        """Fetch persisted recommendations from DB."""
        recs = (
            db.query(UserRecommendation)
            .filter(UserRecommendation.user_id == user.id)
            .order_by(UserRecommendation.score.desc())
            .all()
        )
        results = []
        for r in recs:
            try:
                results.append(SongResponse.model_validate_json(r.data))
            except Exception:
                continue
        return results

    async def build_home_data(
        self,
        db: Session,
        user: Optional[User] = None,
        limit: int = 30,
        proxy_base: Optional[str] = None,
    ) -> HomeResponse:
        logger.info(f"Building home data (parallel) for user: {user.username if user else 'anon'}")
        
        try:
            ytm = self.get_client(user)
        except Exception as e:
            logger.error(f"Failed to get client: {e}")
            raise HTTPException(status_code=401, detail="YouTube Music not connected")

        # ── Parallel Fetching ───────────────────────────────────────────────────
        # Use asyncio.gather to fetch all shelves in parallel via worker threads
        
        async def fetch_home():
            try:
                return await run_sync(ytm.get_home, limit)
            except Exception as e:
                logger.warning(f"Failed to fetch home shelves: {e}")
                return []

        async def fetch_liked():
            try:
                return await run_sync(ytm.get_liked_songs, 24)
            except Exception as e:
                logger.warning(f"Failed to fetch liked songs: {e}")
                return {"tracks": []}

        async def fetch_history():
            try:
                return await run_sync(ytm.get_history)
            except Exception as e:
                logger.warning(f"Failed to fetch history: {e}")
                return []

        async def fetch_trending():
            try:
                return await run_sync(self._get_trending_songs, ytm, proxy_base)
            except Exception as e:
                logger.warning(f"Failed to fetch trending: {e}")
                return []

        # Trigger all calls concurrently
        home_task, liked_task, history_task, trending_task = await asyncio.gather(
            fetch_home(),
            fetch_liked(),
            fetch_history(),
            fetch_trending()
        )

        shelves_list = []
        seen_ids = set()

        # 1. Process Home Shelves
        for raw_shelf in home_task:
            if not isinstance(raw_shelf, dict):
                continue
            title = raw_shelf.get("title", "Untitled")
            contents = raw_shelf.get("contents") or []

            items = []
            for item in contents:
                if not isinstance(item, dict):
                    continue
                video_id = item.get("videoId")
                if video_id:
                    if video_id in seen_ids:
                        continue
                    seen_ids.add(video_id)
                    try:
                        song = normalize_song(item, proxy_base)
                        if song:
                            items.append({"type": "song", "data": song.model_dump()})
                    except Exception:
                        pass
                elif is_artist_item(item):
                    try:
                        artist = normalize_artist(item, proxy_base)
                        if artist:
                            items.append({"type": "artist", "data": artist.model_dump()})
                    except Exception:
                        pass
                elif "playlistId" in item or "browseId" in item:
                    is_album = str(item.get("browseId", "")).startswith("MPREb")
                    try:
                        playlist = normalize_playlist(item, proxy_base)
                        if playlist:
                            items.append({
                                "type": "album" if is_album else "playlist",
                                "data": playlist.model_dump(),
                            })
                    except Exception:
                        pass

            if items:
                section = self._classify_shelf(title) or "musicForYou"
                shelves_list.append({"title": title, "section": section, "items": items})

        # 2. Local RecSys ("Fresh Picks")
        fresh_finds = []
        if user:
            # lightweight sync check
            fresh_finds = self._get_fresh_picks_local(db, user, proxy_base)
            if not fresh_finds:
                # Synchronous fallback if missing
                fresh_finds = self.generate_recommendations(db, user, ytm, proxy_base)

        # 3. Post-Process Fallbacks
        quick_picks = [i["data"] for s in shelves_list if s["section"] == "quickPicks" for i in s["items"] if i["type"] == "song"]
        if not quick_picks:
            for item in liked_task.get("tracks", []):
                song = normalize_song(item, proxy_base)
                if song:
                    quick_picks.append(song)

        listen_again = [i["data"] for s in shelves_list if s["section"] == "listeningAgain" for i in s["items"] if i["type"] == "song"]
        if not listen_again:
            for item in history_task[:20]:
                song = normalize_song(item, proxy_base)
                if song:
                    listen_again.append(song)

        trending = trending_task

        # 4. Final HomeResponse Construction
        ordered_shelves = []
        if quick_picks:
            ordered_shelves.append({
                "title": "Quick picks",
                "section": "quickPicks",
                "items": [{"type": "song", "data": s if isinstance(s, dict) else s.model_dump()} for s in quick_picks]
            })
        if listen_again:
            ordered_shelves.append({
                "title": "Listen again",
                "section": "listeningAgain",
                "items": [{"type": "song", "data": s if isinstance(s, dict) else s.model_dump()} for s in listen_again]
            })
        if fresh_finds:
            ordered_shelves.append({
                "title": "Fresh picks for you",
                "section": "freshFinds",
                "items": [{"type": "song", "data": s if isinstance(s, dict) else s.model_dump()} for s in fresh_finds]
            })
        if trending:
            ordered_shelves.append({
                "title": "Trending",
                "section": "trending",
                "items": [{"type": "song", "data": s if isinstance(s, dict) else s.model_dump()} for s in trending]
            })

        seen_sections = {"quickPicks", "listeningAgain", "freshFinds", "trending"}
        for s in shelves_list:
            if s["section"] not in seen_sections:
                ordered_shelves.append(s)

        profile_url = user.yt_avatar_url if user and user.yt_avatar_url else f"https://api.dicebear.com/7.x/avataaars/svg?seed={user.username if user else 'anon'}"
        
        return HomeResponse(
            shelves=ordered_shelves,
            trending=trending,
            profileUrl=profile_url,
            yt_name=user.yt_name if user else None,
            quickAccess=[s if isinstance(s, SongResponse) else SongResponse.model_validate(s) for s in quick_picks],
            listeningAgain=[s if isinstance(s, SongResponse) else SongResponse.model_validate(s) for s in listen_again],
            freshFinds=[s if isinstance(s, SongResponse) else SongResponse.model_validate(s) for s in fresh_finds],
        )

    def get_user_profile(self, user: User) -> dict:
        try:
            ytm = self.get_client(user)
            # get_account_info() is not a standard ytmusicapi method in all versions, 
            # but usually available or can be inferred from other calls.
            # In some versions it's get_library_playlists() and checking headers or similar.
            # Let's try to get it via a hack or check official docs.
            # Actually, ytmusicapi 1.11.5 doesn't have a direct get_account_info.
            # But we can get it from the home page or library.
            try:
                # This often contains profile info in the response headers or initial data
                # but ytmusicapi doesn't expose it easily.
                # However, we can try to get it from a specific endpoint if we have OAuth.
                pass
            except:
                pass
            return {"name": user.username, "avatar": None} # Fallback
        except Exception as e:
            logger.error(f"Failed to get user profile: {e}")
            return {}

    async def get_home_cached(
        self,
        db: Session,
        user: Optional[User] = None,
        limit: int = 25,
        proxy_base: Optional[str] = None,
    ) -> HomeResponse:
        # Per-user cache key
        user_id = user.id if user else "anon"
        cache_key = f"home_{user_id}_{limit}"

        # 1. Try Memory Cache
        now = time.monotonic()
        if (
            self.home_cache.get(cache_key)
            and self.home_cache[cache_key].get("ts", 0) + self.home_cache_ttl > now
        ):
            logger.debug(f"Home data memory cache hit for {user_id}")
            return self.home_cache[cache_key]["data"]

        # 2. Try Disk Cache
        cached_data = _api_cache.get(cache_key)
        if cached_data:
            logger.debug(f"Home data disk cache hit for {user_id}")
            response = HomeResponse.model_validate(cached_data)
            self.home_cache[cache_key] = {"ts": now, "data": response}
            return response

        # 3. Build Fresh
        data = await self.build_home_data(db, user, limit, proxy_base)
        
        # Save to both caches
        self.home_cache[cache_key] = {"ts": now, "data": data}
        _api_cache.set(cache_key, data.model_dump(), expire=self.home_cache_ttl)
        
        return data

    async def warm_up_user_cache(self, db: Session, user: User, proxy_base: str):
        """Background task to refresh the user's home data."""
        try:
            logger.info(f"Warming up cache for user: {user.username}")
            # This will trigger build_home_data and update both caches
            await self.get_home_cached(db, user, limit=30, proxy_base=proxy_base)
        except Exception as e:
            logger.error(f"Cache warm up failed for {user.username}: {e}")

    def build_feed_data(self, db: Session, proxy_base: Optional[str] = None) -> HomeResponse:
        ytm = ytmusicapi.YTMusic()
        trending = self._get_trending_songs(ytm, proxy_base)
        shelves = []

        music_for_you_items = []
        try:
            raw_shelves = ytm.get_home(limit=3)
            seen = set()
            for shelf in raw_shelves:
                for item in shelf.get("contents") or []:
                    song = normalize_song(item, proxy_base)
                    if song and song.id not in seen:
                        seen.add(song.id)
                        music_for_you_items.append(
                            {"type": "song", "data": song.model_dump()}
                        )
                        if len(music_for_you_items) >= 20:
                            break
                if len(music_for_you_items) >= 20:
                    break
        except Exception:
            pass

        if music_for_you_items:
            shelves.append(
                {
                    "title": "Music For You",
                    "section": "musicForYou",
                    "items": music_for_you_items,
                }
            )

        return HomeResponse(
            shelves=shelves,
            trending=trending,
        )

    def get_feed_cached(self, db: Session, proxy_base: Optional[str] = None) -> HomeResponse:
        now = time.monotonic()
        cached = self.home_cache.get("feed")
        if cached and cached.get("ts", 0) + self.home_cache_ttl > now:
            return cached["data"]
        data = self.build_feed_data(db, proxy_base)
        self.home_cache["feed"] = {"ts": now, "data": data}
        return data

    def clear_cache(self, user_id: Optional[int] = None):
        if user_id:
            cache_key = f"home_{user_id}"
            if cache_key in self.home_cache:
                del self.home_cache[cache_key]
        else:
            self.home_cache.clear()


# Global cache for extracted audio URLs to avoid slow yt-dlp calls on every request.
# Format: {video_id: (url, expiry_timestamp)}
_url_cache = {}
_failure_cache = {}  # {video_id: expiry_timestamp}
_extraction_locks: Dict[str, asyncio.Lock] = {}

# Keep track of which strategy/cookie combination worked last to try it first next time (Fast Path)
_preferred_strategy_name: Optional[str] = "android_vr"
_preferred_cookie_type: Optional[str] = "global"  # 'user', 'global', or 'none'

try:
    import curl_cffi  # noqa: F401
    from yt_dlp.networking.impersonate import ImpersonateTarget

    # yt-dlp 2025.01.15+ expects an ImpersonateTarget object when using
    # the programmatic API. Passing a string like "chrome" causes an empty
    # AssertionError because it's not pre-parsed as it is in the CLI.
    _IMPERSONATE_TARGET: Optional[ImpersonateTarget] = ImpersonateTarget.from_str(
        "chrome"
    )
    logger.info("curl-cffi available — browser impersonation enabled (chrome)")
except (ImportError, AttributeError):
    _IMPERSONATE_TARGET = None
    logger.warning(
        "curl-cffi not installed or old yt-dlp — browser impersonation disabled. "
        "Rebuild the Docker image: docker compose build --no-cache flow-api"
    )


async def extract_audio_url(video_id: str, user: Optional[User] = None) -> str:
    now = time.monotonic()

    # 1. Check Success Cache
    if video_id in _url_cache:
        url, expiry = _url_cache[video_id]
        if now < expiry:
            logger.debug(f"Cache hit for {video_id}")
            return url

    # 2. Check Failure Cache (short-lived)
    if video_id in _failure_cache:
        if now < _failure_cache[video_id]:
            logger.warning(f"Returning cached failure for {video_id} (throttled)")
            raise Exception(
                f"Extraction previously failed for {video_id}, retrying later."
            )

    logger.info(f"Cache miss for {video_id}, extracting...")

    # Use a lock to prevent concurrent extractions for the same video_id
    if video_id not in _extraction_locks:
        _extraction_locks[video_id] = asyncio.Lock()

    async with _extraction_locks[video_id]:
        # Double-check caches inside the lock
        if video_id in _url_cache:
            url, expiry = _url_cache[video_id]
            if now < expiry:
                return url

        # 1. Build cookie paths and identify types
        user_cookie_path = None
        if user and user.yt_auth_json:
            data_dir = os.path.dirname(settings.COOKIES_FILE_PATH)
            user_cookie_path = os.path.join(data_dir, f"cookies_{user.id}.txt")
            if not write_cookie_file(user.yt_auth_json, user_cookie_path):
                user_cookie_path = None

        global_cookie_path = (
            settings.COOKIES_FILE_PATH
            if os.path.exists(settings.COOKIES_FILE_PATH)
            else None
        )

        def get_cp(ctype):
            if ctype == "user":
                return user_cookie_path
            if ctype == "global":
                return global_cookie_path
            return None

        imp = _IMPERSONATE_TARGET
        strategies = [
            {
                "name": "android_vr",
                "player_clients": ["android_vr"],
                "format": "bestaudio/best",
                "impersonate": imp,
            },
            {
                "name": "android",
                "player_clients": ["android"],
                "format": "bestaudio/best",
                "impersonate": imp,
            },
            {
                "name": "ios",
                "player_clients": ["ios"],
                "format": "bestaudio/best",
                "impersonate": imp,
            },
            {
                "name": "web",
                "player_clients": ["web"],
                "format": "bestaudio/best",
                "impersonate": imp,
            },
            {
                "name": "mweb",
                "player_clients": ["mweb"],
                "format": "bestaudio/best",
                "impersonate": imp,
            },
            {
                "name": "tv_embedded",
                "player_clients": ["tv_embedded"],
                "format": "bestaudio/best",
                "impersonate": imp,
            },
        ]

        # 2. Fast Path: Try the globally preferred strategy first
        global _preferred_strategy_name, _preferred_cookie_type
        if _preferred_strategy_name and _preferred_cookie_type:
            strategy = next(
                (s for s in strategies if s["name"] == _preferred_strategy_name),
                strategies[0],
            )
            cp = get_cp(_preferred_cookie_type)
            # Only try if cookie path is available for that type (except for 'none')
            if _preferred_cookie_type == "none" or cp:
                try:
                    logger.debug(
                        f"Fast-path extraction for {video_id} using {_preferred_strategy_name} ({_preferred_cookie_type})"
                    )
                    url = await run_sync(_single_extract_sync, video_id, strategy, cp)
                    if url:
                        return url
                except Exception:
                    logger.debug(
                        f"Fast-path failed for {video_id}, falling back to parallel"
                    )

        # 3. Parallel Path: Try all combinations
        cookie_types = ["user", "global", "none"]
        trials = []
        for strategy in strategies:
            for ctype in cookie_types:
                cp = get_cp(ctype)
                if cp or ctype == "none":
                    # Avoid re-trying the fast-path combination if we already tried it
                    if (
                        strategy["name"] == _preferred_strategy_name
                        and ctype == _preferred_cookie_type
                    ):
                        continue
                    trials.append((strategy, cp, ctype))

        # --- CRITICAL FIX: Low-Latency Parallel Extraction ---
        # We spawn multiple extraction strategies in parallel tasks.
        # Python synchronous threads (running yt-dlp) cannot be forcefully killed.
        # Using anyio.create_task_group() would wait for ALL tasks to finish (even 
        # the slow failing ones), causing 10-20s latency.
        # Instead, we use asyncio.wait(FIRST_COMPLETED) to return the FIRST successful 
        # result immediately to the user, ensuring music starts playing within 1-2s.
        result_container = []
        worker_tasks = []

        async def worker(s, c, ct):
            try:
                url = await run_sync(_single_extract_sync, video_id, s, c)
                if url and not result_container:
                    # Capture the first successful URL safely
                    result_container.append((url, s["name"], ct))
                    return True
            except Exception:
                pass
            return False

        # Create all tasks
        for s, c, ct in trials:
            worker_tasks.append(asyncio.create_task(worker(s, c, ct)))
            # Slight stagger to prioritize user cookies if available
            if ct == "user":
                await asyncio.sleep(0.05)

        # Wait loop: exits as soon as we have a result or all tasks finish.
        while worker_tasks:
            done, pending = await asyncio.wait(
                worker_tasks, return_when=asyncio.FIRST_COMPLETED
            )
            worker_tasks = list(pending)
            
            if result_container:
                # SUCCESS: Cancel remaining tasks. Note: yt-dlp threads already 
                # inside run_sync will finish in background, but won't block the user.
                for task in worker_tasks:
                    task.cancel()
                break
            
            # If all done tasks failed and no result yet, loop again to wait for pending

        if result_container:
            url, sname, ctype = result_container[0]
            _preferred_strategy_name = sname
            _preferred_cookie_type = ctype
            # Keep pending tasks running in background to warm up caches/cookies
            # but return the result to the user immediately.
            return url

        # All failed
        _failure_cache[video_id] = now + 300  # Block retries for 5 mins
        raise Exception(f"Extraction failed for {video_id} after all strategies.")


def _single_extract_sync(
    video_id: str, strategy: dict, cookie_path: Optional[str]
) -> str:
    """Synchronous trial for a single strategy and cookie path."""
    now = time.monotonic()
    yt_url = f"https://www.youtube.com/watch?v={video_id}"

    logger.debug(
        f"Trying extraction strategy: {strategy['name']} (cookies={'yes' if cookie_path else 'no'}) for {video_id}"
    )

    ydl_opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "nocheckcertificate": True,
        "noplaylist": True,
        "format": strategy["format"],
        "cookiefile": cookie_path,
        "http_headers": {
            "Referer": "https://www.youtube.com/",
        },
        "js_runtimes": {
            "node": {}
        },  # required for signature solving in yt-dlp 2025.01.15+
        "extractor_args": {
            "youtube": {
                "player_client": strategy["player_clients"],
            }
        },
    }
    if strategy.get("impersonate"):
        ydl_opts["impersonate"] = strategy["impersonate"]

    try:
        info = None
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(yt_url, download=False)
        except Exception as fe:
            # If format-specific extraction fails, try one more time with NO format constraint
            # This is slow but better than total failure for this strategy.
            logger.debug(f"Format-specific extraction failed for {strategy['name']}, trying loose fallback: {fe}")
            loose_opts = ydl_opts.copy()
            loose_opts["format"] = None
            with yt_dlp.YoutubeDL(loose_opts) as ydl:
                info = ydl.extract_info(yt_url, download=False)

        if not info:
            raise Exception("No info returned")

        final_url = info.get("url")
        if not final_url:
            # Fallback: scan formats list manually
            audio_only = [
                f
                for f in (info.get("formats") or [])
                if f.get("vcodec") == "none"
                and f.get("acodec") not in (None, "none")
                and f.get("url")
            ]
            audio_only.sort(
                key=lambda f: float(f.get("abr") or f.get("tbr") or 0),
                reverse=True,
            )
            final_url = audio_only[0]["url"] if audio_only else None

        if not final_url:
            raise Exception("No direct URL found in formats")

        _url_cache[video_id] = (final_url, now + 3600)
        logger.info(
            f"Extracted URL ({strategy['name']}, cookies={'yes' if cookie_path else 'no'}) "
            f"for {video_id}: ext={info.get('ext')} abr={info.get('abr')}kbps"
        )
        return final_url

    except Exception as e:
        logger.warning(
            f"Strategy {strategy['name']} (cookies={'yes' if cookie_path else 'no'}) "
            f"failed for {video_id}: {e}"
        )
        raise e


yt_service = YTMusicService()
auth_service = AuthService()
