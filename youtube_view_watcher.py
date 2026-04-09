import argparse
import json
import os
import socket
import struct
import sys
import time
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import urlopen

YOUTUBE_API_URL = "https://www.googleapis.com/youtube/v3/videos"
OSC_NEW_VIEWS_ADDRESS = "/newviews"
OSC_VIEW_CHECK_ADDRESS = "/viewcheck"


def extract_video_id(video_url: str) -> str:
    parsed = urlparse(video_url)
    host = parsed.netloc.lower()

    if host in {"youtu.be", "www.youtu.be"}:
        video_id = parsed.path.lstrip("/")
        if video_id:
            return video_id

    if host in {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
    }:
        if parsed.path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [None])[0]
            if video_id:
                return video_id

        if parsed.path.startswith("/shorts/") or parsed.path.startswith("/live/"):
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) >= 2:
                return parts[1]

        if parsed.path.startswith("/embed/"):
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) >= 2:
                return parts[1]

    raise ValueError(f"Could not extract a video ID from URL: {video_url}")


# MODIFIED: Now accepts a list of IDs and returns a dictionary of {id: views}
def fetch_view_counts(api_key: str, video_ids: list[str]) -> dict[str, int]:
    if not video_ids:
        return {}

    # Join the IDs with commas for a single batched API call
    ids_param = ",".join(video_ids)

    query = f"{YOUTUBE_API_URL}?{urlencode({'part': 'statistics', 'id': ids_param, 'key': api_key})}"

    with urlopen(query, timeout=10) as response:
        payload = json.load(response)

    items = payload.get("items", [])

    # Map video ID to its view count
    results = {}
    for item in items:
        v_id = item.get("id")
        statistics = item.get("statistics", {})
        view_count = statistics.get("viewCount")
        if v_id and view_count is not None:
            results[v_id] = int(view_count)

    return results


def discover_broadcast_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        local_ip = sock.getsockname()[0]
    finally:
        sock.close()

    octets = local_ip.split(".")
    if len(octets) != 4:
        raise ValueError(
            f"Could not determine a valid IPv4 address from {local_ip}"
        )

    octets[-1] = "255"
    return ".".join(octets)


def _osc_pad(data: bytes) -> bytes:
    padding = (4 - (len(data) % 4)) % 4
    return data + (b"\x00" * padding)


def build_osc_message(address: str, value: int) -> bytes:
    if not -(2**31) <= value < 2**31:
        raise ValueError(
            "OSC integer value must fit in a 32-bit signed integer"
        )

    address_part = _osc_pad(address.encode("utf-8") + b"\x00")
    type_tag_part = _osc_pad(b",i\x00")
    value_part = struct.pack(">i", value)
    return address_part + type_tag_part + value_part


def send_osc_broadcast(
    address: str, value: int, broadcast_ip: str, port: int
) -> None:
    packet = build_osc_message(address, value)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(packet, (broadcast_ip, port))
    finally:
        sock.close()


# MODIFIED: Now iterates through multiple videos and handles OSC mapping
def watch_videos(
    api_key: str,
    video_urls: list[str],
    port: int,
    interval_seconds: float,
    broadcast_ip: Optional[str],
) -> None:
    # Extract IDs for all URLs
    video_ids = [extract_video_id(url) for url in video_urls]

    # API hard limit is 50 per request
    if len(video_ids) > 50:
        print(
            "⚠️ Warning: YouTube API limits batching to 50 videos. Only tracking the first 50."
        )
        video_ids = video_ids[:50]

    target_ip = broadcast_ip or discover_broadcast_ip()

    # Dictionary to keep track of previous view counts for each video
    last_counts: dict[str, int] = {}

    print(f"Watching {len(video_ids)} videos...")
    print(
        f"OSC target: {target_ip}:{port} ({OSC_NEW_VIEWS_ADDRESS}, {OSC_VIEW_CHECK_ADDRESS})"
    )
    print(f"Poll interval: {interval_seconds} seconds")

    while True:
        try:
            current_counts = fetch_view_counts(api_key, video_ids)

            total_delta = 0
            any_new_views = False

            for v_id in video_ids:
                current_count = current_counts.get(v_id)

                if current_count is None:
                    print(
                        f"[{v_id}] Not found or API key does not have access."
                    )
                    continue

                last_count = last_counts.get(v_id)

                if last_count is None:
                    print(f"[{v_id}] Initial views: {current_count}")
                elif current_count > last_count:
                    delta = current_count - last_count
                    total_delta += delta
                    any_new_views = True
                    print(f"[{v_id}] new views: +{delta} (total: {current_count})")
                elif current_count == last_count:
                    print(f"[{v_id}] same views")
                else:
                    print(
                        f"[{v_id}] view count decreased from {last_count} to {current_count}"
                    )

                # Save the new count for the next loop
                last_counts[v_id] = current_count

            # Send a single OSC message for the entire batch
            if any_new_views:
                send_osc_broadcast(OSC_NEW_VIEWS_ADDRESS, total_delta, target_ip, port)
                send_osc_broadcast(OSC_VIEW_CHECK_ADDRESS, 1, target_ip, port)
                print(f"-> OSC broadcast: {OSC_NEW_VIEWS_ADDRESS}={total_delta}, {OSC_VIEW_CHECK_ADDRESS}=1")
            elif last_counts:
                send_osc_broadcast(OSC_VIEW_CHECK_ADDRESS, 0, target_ip, port)
                print(f"-> OSC broadcast: {OSC_VIEW_CHECK_ADDRESS}=0")

        except KeyboardInterrupt:
            print("\nStopped.")
            return
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)

        time.sleep(interval_seconds)


# MODIFIED: Changed video_url to video_urls with nargs='+'
def parse_args() -> argparse.Namespace:
    def positive_float(value: str) -> float:
        parsed = float(value)
        if parsed <= 0:
            raise argparse.ArgumentTypeError(
                "interval must be greater than 0"
            )
        return parsed

    def udp_port(value: str) -> int:
        parsed = int(value)
        if not 1 <= parsed <= 65535:
            raise argparse.ArgumentTypeError(
                "port must be between 1 and 65535"
            )
        return parsed

    parser = argparse.ArgumentParser(
        description=(
            "Poll multiple YouTube videos' view counts and broadcast OSC messages for "
            "new-view deltas and per-check status."
        )
    )
    # Changed here: accepts 1 or more URLs separated by spaces
    parser.add_argument(
        "video_urls", nargs="+", help="YouTube video URLs to watch (space-separated)"
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("YOUTUBE_API_KEY"),
        help="YouTube Data API v3 key. Defaults to YOUTUBE_API_KEY if set.",
    )
    parser.add_argument(
        "--port",
        type=udp_port,
        default=5005,
        help="UDP port used for OSC broadcast messages (default: 5005)",
    )
    parser.add_argument(
        "--interval",
        type=positive_float,
        default=3.0,
        help="Polling interval in seconds (default: 3)",
    )
    parser.add_argument(
        "--broadcast-ip",
        help=(
            "Broadcast IPv4 address. Defaults to the current local subnet with "
            "the last octet replaced by 255."
        ),
    )
    args = parser.parse_args()
    if not args.api_key:
        parser.error("--api-key is required unless YOUTUBE_API_KEY is set")
    return args


if __name__ == "__main__":
    args = parse_args()
    watch_videos(
        api_key=args.api_key,
        video_urls=args.video_urls,
        port=args.port,
        interval_seconds=args.interval,
        broadcast_ip=args.broadcast_ip,
    )