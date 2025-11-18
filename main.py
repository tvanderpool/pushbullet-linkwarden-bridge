#!/usr/bin/env python3
"""
Pushbullet to Linkwarden Bridge
Listens to Pushbullet pushes and automatically saves links to Linkwarden collections.
"""

import json
import logging
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import requests
import websocket
from bs4 import BeautifulSoup
from ruamel.yaml import YAML

# Constants
PUSHBULLET_WS_URL = "wss://stream.pushbullet.com/websocket/{token}"
PUSHBULLET_API_BASE = "https://api.pushbullet.com/v2"
PROCESSED_PUSHES_FILE = "processed_pushes.json"
CONFIG_FILE = "config.yaml"
FIREFOX_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0"


@dataclass
class AppSettings:
    """Application settings"""
    dry_run: bool = False
    log_level: str = "INFO"
    reconnect_max_delay: int = 300
    reconnect_initial_delay: int = 2
    request_timeout: int = 30


class PushbulletLinkwardenBridge:
    """Main application class for bridging Pushbullet and Linkwarden"""

    def __init__(self, config_path: str = CONFIG_FILE):
        self.config_path = config_path
        self.config: Dict[str, Any] = {}
        self.settings = AppSettings()
        self.running = True
        self.ws: Optional[websocket.WebSocketApp] = None
        self.processed_pushes: Dict[str, float] = {}  # device_iden -> last_modified
        self.logger = self._setup_logging()
        self.reconnect_delay = 2

        # Setup signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _setup_logging(self) -> logging.Logger:
        """Setup structured logging"""
        logger = logging.getLogger("pushbullet-linkwarden-bridge")
        handler = logging.StreamHandler()
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        return logger

    def _signal_handler(self, signum: int, frame: Any) -> None:
        """Handle shutdown signals gracefully"""
        self.logger.info(f"Received signal {signum}, shutting down gracefully...")
        self.running = False
        if self.ws:
            self.ws.close()
        sys.exit(0)

    def load_config(self) -> None:
        """Load configuration from YAML file"""
        self.logger.info(f"Loading configuration from {self.config_path}")
        yaml = YAML()
        yaml.preserve_quotes = True
        yaml.default_flow_style = False

        try:
            with open(self.config_path, 'r') as f:
                self.config = yaml.load(f)
        except FileNotFoundError:
            self.logger.error(f"Configuration file not found: {self.config_path}")
            self.logger.error("Please copy config.example.yaml to config.yaml and configure it")
            sys.exit(1)
        except Exception as e:
            self.logger.error(f"Error loading configuration: {e}")
            sys.exit(1)

        # Load settings if present
        if 'settings' in self.config:
            settings = self.config['settings']
            self.settings.dry_run = settings.get('dry_run', False)
            self.settings.log_level = settings.get('log_level', 'INFO')
            self.settings.reconnect_max_delay = settings.get('reconnect_max_delay', 300)
            self.settings.reconnect_initial_delay = settings.get('reconnect_initial_delay', 2)
            self.settings.request_timeout = settings.get('request_timeout', 30)

            # Update log level
            self.logger.setLevel(getattr(logging, self.settings.log_level.upper()))

        if self.settings.dry_run:
            self.logger.info("DRY RUN MODE: Links will not be created in Linkwarden")

    def save_config(self) -> None:
        """Save configuration to YAML file (preserving comments)"""
        self.logger.info(f"Updating configuration file: {self.config_path}")
        yaml = YAML()
        yaml.preserve_quotes = True
        yaml.default_flow_style = False

        try:
            with open(self.config_path, 'w') as f:
                yaml.dump(self.config, f)
        except Exception as e:
            self.logger.error(f"Error saving configuration: {e}")

    def validate_config(self) -> bool:
        """Validate configuration"""
        self.logger.info("Validating configuration")

        if 'linkwarden' not in self.config:
            self.logger.error("Missing 'linkwarden' section in config")
            return False

        if 'pushbullet' not in self.config:
            self.logger.error("Missing 'pushbullet' section in config")
            return False

        if not self.config['linkwarden'].get('api_url'):
            self.logger.error("Missing linkwarden.api_url in config")
            return False

        if not self.config['linkwarden'].get('api_token'):
            self.logger.error("Missing linkwarden.api_token in config")
            return False

        if not self.config['pushbullet'].get('api_token'):
            self.logger.error("Missing pushbullet.api_token in config")
            return False

        if 'channels' not in self.config or not self.config['channels']:
            self.logger.error("No channels configured")
            return False

        return True

    def _make_pushbullet_request(self, method: str, endpoint: str, **kwargs) -> Optional[requests.Response]:
        """Make authenticated request to Pushbullet API"""
        url = f"{PUSHBULLET_API_BASE}/{endpoint}"
        headers = {
            'Authorization': f"Bearer {self.config['pushbullet']['api_token']}",
            'Content-Type': 'application/json'
        }

        try:
            response = requests.request(
                method,
                url,
                headers=headers,
                timeout=self.settings.request_timeout,
                **kwargs
            )
            response.raise_for_status()
            return response
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Pushbullet API error ({method} {endpoint}): {e}")
            return None

    def _make_linkwarden_request(self, method: str, endpoint: str, **kwargs) -> Optional[requests.Response]:
        """Make authenticated request to Linkwarden API"""
        url = f"{self.config['linkwarden']['api_url']}/api/v1/{endpoint}"
        headers = {
            'Authorization': f"Bearer {self.config['linkwarden']['api_token']}",
            'Content-Type': 'application/json'
        }

        try:
            response = requests.request(
                method,
                url,
                headers=headers,
                timeout=self.settings.request_timeout,
                **kwargs
            )
            response.raise_for_status()
            return response
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Linkwarden API error ({method} {endpoint}): {e}")
            return None

    def fetch_pushbullet_devices(self) -> List[Dict[str, Any]]:
        """Fetch all Pushbullet devices"""
        self.logger.info("Fetching Pushbullet devices")
        response = self._make_pushbullet_request('GET', 'devices')

        if response:
            devices = response.json().get('devices', [])
            self.logger.info(f"Found {len(devices)} Pushbullet devices")
            return devices
        return []

    def create_pushbullet_device(self, nickname: str) -> Optional[str]:
        """Create a new Pushbullet device"""
        self.logger.info(f"Creating Pushbullet device: {nickname}")
        response = self._make_pushbullet_request(
            'POST',
            'devices',
            json={'nickname': nickname, 'icon': 'system'}
        )

        if response:
            device_iden = response.json().get('iden')
            self.logger.info(f"Created device {nickname} with iden: {device_iden}")
            return device_iden
        return None

    def resolve_devices(self) -> None:
        """Match device names to idens, create if missing"""
        self.logger.info("Resolving Pushbullet devices")
        devices = self.fetch_pushbullet_devices()
        device_map = {d.get('nickname'): d.get('iden') for d in devices if d.get('nickname')}

        config_updated = False
        for channel in self.config['channels']:
            device_name = channel.get('name')
            if not device_name:
                continue

            if not channel.get('device_iden'):
                # Try to find existing device
                if device_name in device_map:
                    channel['device_iden'] = device_map[device_name]
                    self.logger.info(f"Matched device '{device_name}' to iden: {channel['device_iden']}")
                    config_updated = True
                else:
                    # Create new device
                    device_iden = self.create_pushbullet_device(device_name)
                    if device_iden:
                        channel['device_iden'] = device_iden
                        config_updated = True

        if config_updated:
            self.save_config()

    def fetch_linkwarden_collections(self) -> List[Dict[str, Any]]:
        """Fetch all Linkwarden collections"""
        self.logger.info("Fetching Linkwarden collections")
        response = self._make_linkwarden_request('GET', 'collections')

        if response:
            collections = response.json().get('response', [])
            self.logger.info(f"Found {len(collections)} Linkwarden collections")
            return collections
        return []

    def create_linkwarden_collection(self, name: str) -> Optional[int]:
        """Create a new Linkwarden collection"""
        self.logger.info(f"Creating Linkwarden collection: {name}")
        response = self._make_linkwarden_request(
            'POST',
            'collections',
            json={'name': name, 'description': 'Auto-created by Pushbullet bridge'}
        )

        if response:
            collection_id = response.json().get('response', {}).get('id')
            self.logger.info(f"Created collection '{name}' with id: {collection_id}")
            return collection_id
        return None

    def resolve_collections(self) -> None:
        """Match collection names to IDs, create if missing"""
        self.logger.info("Resolving Linkwarden collections")
        collections = self.fetch_linkwarden_collections()
        collection_map = {c.get('name'): c.get('id') for c in collections if c.get('name')}

        config_updated = False
        for channel in self.config['channels']:
            collection_name = channel.get('collection')
            if not collection_name:
                continue

            if not channel.get('collection_id'):
                # Try to find existing collection
                if collection_name in collection_map:
                    channel['collection_id'] = collection_map[collection_name]
                    self.logger.info(f"Matched collection '{collection_name}' to id: {channel['collection_id']}")
                    config_updated = True
                else:
                    # Create new collection
                    collection_id = self.create_linkwarden_collection(collection_name)
                    if collection_id:
                        channel['collection_id'] = collection_id
                        config_updated = True

        if config_updated:
            self.save_config()

    def load_processed_pushes(self) -> None:
        """Load history of processed pushes"""
        try:
            if Path(PROCESSED_PUSHES_FILE).exists():
                with open(PROCESSED_PUSHES_FILE, 'r') as f:
                    self.processed_pushes = json.load(f)
                self.logger.info(f"Loaded {len(self.processed_pushes)} processed push records")
            else:
                self.logger.info("No previous push history found")
        except Exception as e:
            self.logger.error(f"Error loading processed pushes: {e}")
            self.processed_pushes = {}

    def save_processed_pushes(self) -> None:
        """Save history of processed pushes"""
        try:
            with open(PROCESSED_PUSHES_FILE, 'w') as f:
                json.dump(self.processed_pushes, f, indent=2)
        except Exception as e:
            self.logger.error(f"Error saving processed pushes: {e}")

    def get_collection_for_device(self, device_iden: Optional[str]) -> Optional[int]:
        """Get the collection ID for a given device iden"""
        if not device_iden:
            return None

        for channel in self.config['channels']:
            if channel.get('device_iden') == device_iden:
                return channel.get('collection_id')
        return None

    def fetch_recent_pushes(self, modified_after: float = 0) -> List[Dict[str, Any]]:
        """Fetch recent pushes from Pushbullet"""
        self.logger.debug(f"Fetching pushes modified after {modified_after}")
        response = self._make_pushbullet_request(
            'GET',
            'pushes',
            params={'modified_after': modified_after, 'active': 'true', 'limit': 10}
        )

        if response:
            pushes = response.json().get('pushes', [])
            self.logger.debug(f"Fetched {len(pushes)} recent pushes")
            return pushes
        return []

    def process_push(self, push: Dict[str, Any]) -> None:
        """Process a single push and save to Linkwarden if it's a link"""
        push_type = push.get('type')
        push_iden = push.get('iden')
        push_modified = push.get('modified', 0)

        # Only process link pushes
        if push_type != 'link':
            self.logger.debug(f"Skipping non-link push {push_iden} (type: {push_type})")
            return

        # Get device information
        target_device_iden = push.get('target_device_iden')
        source_device_iden = push.get('source_device_iden')

        # Determine which device to use (prefer target, fallback to source)
        device_iden = target_device_iden or source_device_iden

        if not device_iden:
            self.logger.warning(f"Push {push_iden} has no device identifier, skipping")
            return

        # Check if we should process this device
        collection_id = self.get_collection_for_device(device_iden)
        if not collection_id:
            self.logger.debug(f"No collection configured for device {device_iden}, skipping")
            return

        # Check if already processed
        if device_iden in self.processed_pushes:
            last_processed = self.processed_pushes[device_iden]
            if push_modified <= last_processed:
                self.logger.debug(f"Push {push_iden} already processed")
                return

        # Extract link information
        url = push.get('url')
        title = push.get('title', '')
        body = push.get('body', '')

        if not url:
            self.logger.warning(f"Push {push_iden} has no URL, skipping")
            return

        # Resolve URL redirects (particularly for search.app URLs)
        resolved_url = self._resolve_url(url)

        title = self._extract_page_title(resolved_url) or title

        # Save to Linkwarden with resolved URL
        self.save_to_linkwarden(resolved_url, title, body, collection_id, device_iden)

        # Update processed pushes
        self.processed_pushes[device_iden] = push_modified
        self.save_processed_pushes()

    def save_to_linkwarden(self, url: str, title: str, description: str,
                          collection_id: int, device_iden: str) -> None:
        """Save a link to Linkwarden"""
        device_name = self._get_device_name(device_iden)
        collection_name = self._get_collection_name(collection_id)

        self.logger.info(
            f"Saving link to Linkwarden: '{title or url}' "
            f"(device: {device_name}, collection: {collection_name})"
        )

        if self.settings.dry_run:
            self.logger.info(f"[DRY RUN] Would save: {url}")
            return

        link_data = {
            'url': url,
            'name': title or url,
            'description': description,
            'collection': {'id': collection_id}
        }

        response = self._make_linkwarden_request('POST', 'links', json=link_data)

        if response:
            self.logger.info(f"Successfully saved link: {title or url}")
        else:
            self.logger.error(f"Failed to save link: {title or url}")

    def _get_device_name(self, device_iden: str) -> str:
        """Get device name from iden"""
        for channel in self.config['channels']:
            if channel.get('device_iden') == device_iden:
                return channel.get('name', device_iden)
        return device_iden

    def _get_collection_name(self, collection_id: int) -> str:
        """Get collection name from ID"""
        for channel in self.config['channels']:
            if channel.get('collection_id') == collection_id:
                return channel.get('collection', str(collection_id))
        return str(collection_id)

    def _extract_page_title(self, url: str) -> Optional[str]:
        """Extract page title from URL using beautifulsoup4"""
        try:
            headers = {'User-Agent': FIREFOX_USER_AGENT}
            response = requests.get(
                url,
                headers=headers,
                timeout=self.settings.request_timeout,
                allow_redirects=True
            )
            response.raise_for_status()

            soup = BeautifulSoup(response.content, 'html.parser')
            title_tag = soup.find('title')

            if title_tag and title_tag.string:
                title = title_tag.string.strip()
                self.logger.debug(f"Extracted title from {url}: {title}")
                return title

            return None
        except Exception as e:
            self.logger.warning(f"Failed to extract title from {url}: {e}")
            return None

    def _resolve_url(self, url: str) -> str:
        """
        Resolve URL redirects, particularly for search.app URLs.
        """
        if not url.startswith('https://search.app/'):
            # Not a search.app URL, return as-is
            return url

        self.logger.info(f"Resolving search.app URL: {url}")

        try:
            headers = {'User-Agent': FIREFOX_USER_AGENT}

            # Try HEAD request first to get the redirect location
            response = requests.head(
                url,
                headers=headers,
                timeout=self.settings.request_timeout,
                allow_redirects=False
            )

            # If HEAD fails with 403, try GET instead (some services block HEAD)
            if response.status_code == 403:
                self.logger.debug("HEAD request returned 403, trying GET instead")
                response = requests.get(
                    url,
                    headers=headers,
                    timeout=self.settings.request_timeout,
                    allow_redirects=False
                )

            if response.status_code in (301, 302, 303, 307, 308):
                resolved_url = response.headers.get('location')
                if resolved_url:
                    self.logger.info(f"Resolved {url} -> {resolved_url}")
                    return resolved_url

            # If no redirect found, return original URL
            self.logger.warning(f"No redirect found for {url} (status: {response.status_code})")
            return url

        except Exception as e:
            self.logger.error(f"Failed to resolve URL {url}: {e}")
            return url

    def process_initial_pushes(self) -> None:
        """Process pushes since last run for tracked devices"""
        self.logger.info("Processing pushes since last run")

        for channel in self.config['channels']:
            device_iden = channel.get('device_iden')
            if not device_iden:
                continue

            # Check if this is a newly added device (not in processed history)
            if device_iden not in self.processed_pushes:
                # For new devices, just get the latest push timestamp without processing
                self.logger.info(
                    f"Device '{channel.get('name')}' is new, "
                    "recording current state without processing old pushes"
                )
                pushes = self.fetch_recent_pushes(modified_after=0)
                link_pushes = [p for p in pushes if p.get('type') == 'link'
                              and (p.get('target_device_iden') == device_iden
                                   or p.get('source_device_iden') == device_iden)]

                if link_pushes:
                    # Record the most recent push timestamp
                    latest_modified = max(p.get('modified', 0) for p in link_pushes)
                    self.processed_pushes[device_iden] = latest_modified
                    self.logger.info(
                        f"Recorded {len(link_pushes)} existing pushes for new device "
                        f"'{channel.get('name')}' (not processing them)"
                    )
                else:
                    # No pushes, set to current time
                    self.processed_pushes[device_iden] = time.time()

                self.save_processed_pushes()
                continue

            # For existing devices, process new pushes since last run
            last_modified = self.processed_pushes[device_iden]
            pushes = self.fetch_recent_pushes(modified_after=last_modified)

            # Filter for this device
            device_pushes = [
                p for p in pushes
                if (p.get('target_device_iden') == device_iden
                    or p.get('source_device_iden') == device_iden)
            ]

            if device_pushes:
                self.logger.info(
                    f"Processing {len(device_pushes)} new pushes for "
                    f"device '{channel.get('name')}'"
                )
                for push in device_pushes:
                    self.process_push(push)

    def on_websocket_message(self, ws: websocket.WebSocketApp, message: str) -> None:
        """Handle incoming WebSocket messages"""
        try:
            data = json.loads(message)
            msg_type = data.get('type')

            self.logger.debug(f"WebSocket message: {msg_type}")

            if msg_type == 'tickle' and data.get('subtype') == 'push':
                self.logger.info("Received push tickle, fetching recent pushes")
                pushes = self.fetch_recent_pushes(modified_after=0)

                for push in pushes:
                    self.process_push(push)

        except json.JSONDecodeError:
            self.logger.error(f"Invalid JSON received: {message}")
        except Exception as e:
            self.logger.error(f"Error processing WebSocket message: {e}", exc_info=True)

    def on_websocket_error(self, ws: websocket.WebSocketApp, error: Exception) -> None:
        """Handle WebSocket errors"""
        self.logger.error(f"WebSocket error: {error}")

    def on_websocket_close(self, ws: websocket.WebSocketApp, close_status_code: int,
                          close_msg: str) -> None:
        """Handle WebSocket close"""
        self.logger.warning(
            f"WebSocket closed (status: {close_status_code}, msg: {close_msg})"
        )

    def on_websocket_open(self, ws: websocket.WebSocketApp) -> None:
        """Handle WebSocket open"""
        self.logger.info("WebSocket connection established")
        # Reset reconnect delay on successful connection
        self.reconnect_delay = self.settings.reconnect_initial_delay

    def connect_to_pushbullet_stream(self) -> None:
        """Connect to Pushbullet WebSocket stream"""
        token = self.config['pushbullet']['api_token']
        ws_url = PUSHBULLET_WS_URL.format(token=token)

        self.logger.info("Connecting to Pushbullet stream")

        self.ws = websocket.WebSocketApp(
            ws_url,
            on_message=self.on_websocket_message,
            on_error=self.on_websocket_error,
            on_close=self.on_websocket_close,
            on_open=self.on_websocket_open
        )

        # Run WebSocket in blocking mode
        self.ws.run_forever()

    def run(self) -> None:
        """Main application loop"""
        self.logger.info("Starting Pushbullet-Linkwarden Bridge")

        # Load and validate configuration
        self.load_config()
        if not self.validate_config():
            self.logger.error("Configuration validation failed")
            sys.exit(1)

        # Resolve devices and collections
        self.resolve_devices()
        self.resolve_collections()

        # Load processed push history
        self.load_processed_pushes()

        # Process any pushes since last run
        self.process_initial_pushes()

        # Main connection loop with reconnection logic
        while self.running:
            try:
                self.connect_to_pushbullet_stream()

                # If we get here, the WebSocket closed normally
                if self.running:
                    self.logger.info(
                        f"Reconnecting in {self.reconnect_delay} seconds..."
                    )
                    time.sleep(self.reconnect_delay)

                    # Exponential backoff
                    self.reconnect_delay = min(
                        self.reconnect_delay * 2,
                        self.settings.reconnect_max_delay
                    )

            except KeyboardInterrupt:
                self.logger.info("Received keyboard interrupt")
                break
            except Exception as e:
                self.logger.error(f"Unexpected error: {e}", exc_info=True)
                if self.running:
                    self.logger.info(
                        f"Reconnecting in {self.reconnect_delay} seconds..."
                    )
                    time.sleep(self.reconnect_delay)

                    # Exponential backoff
                    self.reconnect_delay = min(
                        self.reconnect_delay * 2,
                        self.settings.reconnect_max_delay
                    )

        self.logger.info("Bridge stopped")


def main():
    """Entry point"""
    bridge = PushbulletLinkwardenBridge()
    bridge.run()


if __name__ == '__main__':
    main()
