import ytmusicapi
import json
import os
import sys

def debug_feed():
    auth_path = './flow-source/data/auth.json'
    if not os.path.exists(auth_path):
        print(f"Auth file not found at {auth_path}")
        # Try without auth
        ytm = ytmusicapi.YTMusic()
    else:
        ytm = ytmusicapi.YTMusic(auth_path)

    print("--- FETCHING HOME ---")
    try:
        home = ytm.get_home(limit=5)
        for i, shelf in enumerate(home):
            print(f"\nShelf {i}: {shelf.get('title')} (type: {shelf.get('contents', [{}])[0].get('type') if shelf.get('contents') else 'N/A'})")
            if shelf.get('contents'):
                item = shelf['contents'][0]
                print(f"  First Item Sample:")
                print(f"    Title: {item.get('title')}")
                print(f"    VideoType: {item.get('videoType')}")
                print(f"    ResultType: {item.get('resultType')}")
                if item.get('thumbnails'):
                    print(f"    Thumbnails: {len(item['thumbnails'])} provided")
                    print(f"    First Thumb URL: {item['thumbnails'][0].get('url')[:100]}...")
    except Exception as e:
        print(f"Error fetching home: {e}")

    print("\n--- FETCHING EXPLORE ---")
    try:
        explore = ytm.get_explore()
        print(f"Keys in explore: {list(explore.keys())}")
        if 'trending' in explore:
            items = explore['trending'].get('items', [])
            print(f"Trending items: {len(items)}")
            if items:
                item = items[0]
                print(f"  First Trending Sample:")
                print(f"    Title: {item.get('title')}")
                print(f"    VideoType: {item.get('videoType')}")
                print(f"    Thumbnails: {item.get('thumbnails', [{}])[0].get('url')[:100] if item.get('thumbnails') else 'None'}")
    except Exception as e:
        print(f"Error fetching explore: {e}")

if __name__ == "__main__":
    debug_feed()
