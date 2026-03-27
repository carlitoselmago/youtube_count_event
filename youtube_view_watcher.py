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


def fetch_view_count(api_key: str, video_id: str) -> int:
    query = (
        f"{YOUTUBE_API_URL}?"
        f"{urlencode({'part': 'statistics', 'id': video_id, 'key': api_key})}"
    )

    with urlopen(query, timeout=10) as response:
        payload = json.load(response)

    items = payload.get("items", [])
    if not items:
        raise ValueError("Video not found or API key does not have access.")

    statistics = items[0].get("statistics", {})
    view_count = statistics.get("viewCount")
    if view_count is None:
        raise ValueError("YouTube API response did not include viewCount.")

    return int(view_count)


def discover_broadcast_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        local_ip = sock.getsockname()[0]
    finally:
        sock.close()

    octets = local_ip.split(".")
    if len(octets) != 4:
        raise ValueError(f"Could not determine a valid IPv4 address from {local_ip}")

    octets[-1] = "255"
    return ".".join(octets)


def _osc_pad(data: bytes) -> bytes:
    padding = (4 - (len(data) % 4)) % 4
    return data + (b"\x00" * padding)


def build_osc_message(address: str, value: int) -> bytes:
    if not -(2**31) <= value < 2**31:
        raise ValueError("OSC integer value must fit in a 32-bit signed integer")

    address_part = _osc_pad(address.encode("utf-8") + b"\x00")
    type_tag_part = _osc_pad(b",i\x00")
    value_part = struct.pack(">i", value)
    return address_part + type_tag_part + value_part


def send_osc_broadcast(address: str, value: int, broadcast_ip: str, port: int) -> None:
    packet = build_osc_message(address, value)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(packet, (broadcast_ip, port))
    finally:
        sock.close()


def watch_video(
    api_key: str,
    video_url: str,
    port: int,
    interval_seconds: float,
    broadcast_ip: Optional[str],
) -> None:
    video_id = extract_video_id(video_url)
    target_ip = broadcast_ip or discover_broadcast_ip()
    last_count: Optional[int] = None

    print(f"Watching video {video_id}")
    print(
        f"OSC target: {target_ip}:{port} "
        f"({OSC_NEW_VIEWS_ADDRESS}, {OSC_VIEW_CHECK_ADDRESS})"
    )
    print(f"Poll interval: {interval_seconds} seconds")

    while True:
        try:
            current_count = fetch_view_count(api_key, video_id)

            if last_count is None:
                send_osc_broadcast(OSC_VIEW_CHECK_ADDRESS, 0, target_ip, port)
                print(f"Initial views: {current_count}")
            elif current_count > last_count:
                delta = current_count - last_count
                send_osc_broadcast(OSC_NEW_VIEWS_ADDRESS, delta, target_ip, port)
                send_osc_broadcast(OSC_VIEW_CHECK_ADDRESS, 1, target_ip, port)
                print(
                    f"new views: {delta} (total: {current_count}) -> sent OSC broadcasts"
                )
            elif current_count == last_count:
                send_osc_broadcast(OSC_VIEW_CHECK_ADDRESS, 0, target_ip, port)
                print("same views")
            else:
                send_osc_broadcast(OSC_VIEW_CHECK_ADDRESS, 0, target_ip, port)
                print(
                    f"view count decreased from {last_count} to {current_count}; skipping broadcast"
                )

            last_count = current_count
        except KeyboardInterrupt:
            print("\nStopped.")
            return
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)

        time.sleep(interval_seconds)


def parse_args() -> argparse.Namespace:
    def positive_float(value: str) -> float:
        parsed = float(value)
        if parsed <= 0:
            raise argparse.ArgumentTypeError("interval must be greater than 0")
        return parsed

    def udp_port(value: str) -> int:
        parsed = int(value)
        if not 1 <= parsed <= 65535:
            raise argparse.ArgumentTypeError("port must be between 1 and 65535")
        return parsed

    parser = argparse.ArgumentParser(
        description=(
            "Poll a YouTube video's view count and broadcast OSC messages for "
            "new-view deltas and per-check status."
        )
    )
    parser.add_argument("video_url", help="YouTube video URL to watch")
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
    watch_video(
        api_key=args.api_key,
        video_url=args.video_url,
        port=args.port,
        interval_seconds=args.interval,
        broadcast_ip=args.broadcast_ip,
    )
