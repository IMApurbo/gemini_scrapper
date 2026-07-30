"""Entry point: python -m gemini_scrapper"""
import argparse
import os

from .config import CONFIG, load_config, find_config
from .models import MODELS
from .gemini import HAS_HTTPX
from .server import GeminiHandler, ThreadedServer
from . import __version__


def main():
    parser = argparse.ArgumentParser(description="Gemini Web to OpenAI API")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--config", type=str, default=None)
    parser.add_argument("--cookie-file", type=str, default=None)
    parser.add_argument("--proxy", type=str, default=None, help="HTTP proxy, e.g. http://127.0.0.1:7890")

    fmt_group = parser.add_mutually_exclusive_group()
    fmt_group.add_argument("--openai", action="store_true",
                            help="Serve OpenAI-compatible endpoints (/v1/chat/completions, /v1/responses)")
    fmt_group.add_argument("--anthropic", action="store_true",
                            help="Serve Anthropic-compatible endpoint (/v1/messages) [default]")

    parser.add_argument("--version", action="version", version=f"gemini-scrapper {__version__}")
    args = parser.parse_args()

    config_path = args.config or os.environ.get("GEMINI_SCRAPPER_CONFIG") or find_config()
    if config_path:
        load_config(config_path)

    if args.port:
        CONFIG["port"] = args.port
    if args.cookie_file:
        CONFIG["cookie_file"] = args.cookie_file
    if args.proxy:
        CONFIG["proxy"] = args.proxy
    if args.openai:
        CONFIG["api_format"] = "openai"
    elif args.anthropic:
        CONFIG["api_format"] = "anthropic"
    # else: keep whatever config.json set, defaulting to "anthropic"

    port = CONFIG["port"]
    server = ThreadedServer((CONFIG["host"], port), GeminiHandler)
    api_format = CONFIG.get("api_format", "anthropic")
    endpoint = "/v1/messages" if api_format == "anthropic" else "/v1/chat/completions (+ /v1/responses)"
    print(f"gemini-scrapper v{__version__}")
    print(f"  Listening: http://0.0.0.0:{port}")
    print(f"  Base URL:  http://localhost:{port}/v1")
    print(f"  Format:    {api_format}  ({endpoint})")
    print(f"  Models:    {', '.join(MODELS.keys())}")
    print(f"  Cookie:    {'yes' if CONFIG.get('cookie_file') else 'none (anonymous)'}")
    print(f"  Proxy:     {CONFIG.get('proxy') or 'system env'}")
    print(f"  Streaming: {'httpx (true streaming)' if HAS_HTTPX else 'urllib (buffered)'}")
    print()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.shutdown()


if __name__ == "__main__":
    main()
