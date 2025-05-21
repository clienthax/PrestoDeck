import json
import time
import hashlib
import urequests as requests
import asyncio
import random

# Original code is from https://github.com/jacopo-degattis/spotifyws

# Import external WebSocket client
from lib.ws import AsyncWebsocketClient

# Custom HMAC implementation for MicroPython that matches the original
def hmac_sha1(key, message):
    """Custom HMAC-SHA1 implementation matching the original behavior"""
    block_size = 64  # SHA1 block size in bytes
    
    # Ensure key and message are bytes objects
    if isinstance(key, bytearray):
        key = bytes(key)
    if isinstance(message, bytearray):
        message = bytes(message)
    
    # If key is longer than block size, hash it
    if len(key) > block_size:
        key = hashlib.sha1(key).digest()
    
    # Pad key with zeros to block size
    key = key + b'\x00' * (block_size - len(key))
    
    # Create inner and outer padded keys
    inner_pad = bytes(k ^ 0x36 for k in key)
    outer_pad = bytes(k ^ 0x5c for k in key)
    
    # Compute HMAC: SHA1(outer_pad + SHA1(inner_pad + message))
    inner_hash = hashlib.sha1(inner_pad + message).digest()
    hmac_hash = hashlib.sha1(outer_pad + inner_hash).digest()
    
    return hmac_hash

# Constants from config.py
GET_SPOTIFY_TOKEN = "https://open.spotify.com/get_access_token"
SUBSCRIBE_ACTIVITY = "https://api.spotify.com/v1/me/notifications/user"
REGISTER_DEVICE = "https://guc-spclient.spotify.com/track-playback/v1/devices"
CONNECT_STATE = "https://guc3-spclient.spotify.com/connect-state/v1/devices/"
DEALER_WS_URL = "wss://gew1-dealer.spotify.com"

# TOTP secret from utils.py (keep this exactly as it was)
SPOTIFY_TOTP_SECRET = bytearray([
    53, 53, 48, 55, 49, 52, 53, 56, 53, 51, 52, 56, 55, 52, 57, 57, 
    53, 57, 50, 50, 52, 56, 54, 51, 48, 51, 50, 57, 51, 52, 55
])

class SimpleEventEmitter:
    """Minimal event emitter"""
    def __init__(self):
        self._events = {}
    
    def on(self, event, callback):
        if event not in self._events:
            self._events[event] = []
        self._events[event].append(callback)
    
    def emit(self, event, *args, **kwargs):
        if event in self._events:
            for callback in self._events[event]:
                try:
                    callback(*args, **kwargs)
                except Exception as e:
                    print(f"Error in event callback: {e}")

def generate_device_id():
    """Generate a random device ID"""
    chars = "abcdefghijklmnopqrstuvwxyz0123456789"
    return ''.join(random.choice(chars) for _ in range(40))

DEFAULT_DEVICE = {
    "brand": "spotify",
    "capabilities": {
        "audio_podcasts": True,
        "change_volume": True,
        "disable_connect": False,
        "enable_play_token": True,
        "manifest_formats": [
            "file_urls_mp3",
            "manifest_ids_video",
            "file_urls_external",
            "file_ids_mp4",
            "file_ids_mp4_dual"
        ],
        "play_token_lost_behavior": "pause",
        "supports_file_media_type": True,
        "video_playback": True
    },
    "device_id": generate_device_id(),
    "device_type": "computer",
    "metadata": {},
    "model": "web_player",
    "name": "Web Player (Microsoft Edge)",
    "platform_identifier": "web_player osx 11.3.0;microsoft edge 89.0.774.54;desktop"
}

def generate_totp(secret=SPOTIFY_TOTP_SECRET, digits=6):
    counter = int(time.time()) // 30
    print(f"TOTP counter: {counter} (time: {time.time()})")
    
    # Convert bytearray to bytes for HMAC
    if isinstance(secret, bytearray):
        secret = bytes(secret)
    
    # Use the custom HMAC implementation
    hmac_result = hmac_sha1(secret, counter.to_bytes(8, 'big'))
    
    # Extract the dynamic binary code
    offset = hmac_result[-1] & 15
    truncated_value = (
        (hmac_result[offset] & 127) << 24
        | (hmac_result[offset + 1] & 255) << 16
        | (hmac_result[offset + 2] & 255) << 8
        | (hmac_result[offset + 3] & 255)
    )
    
    # Generate the TOTP
    totp_value = truncated_value % (10**digits)
    totp_str = str(totp_value)
    
    # Manual zero-padding to match original zfill behavior
    while len(totp_str) < digits:
        totp_str = '0' + totp_str
    
    return totp_str, counter * 30_000

# Global variable for NTP sync tracking
_last_ntp_sync = 0

def get_nested_value(obj, path):
    """Get nested value from dict using dot notation (pydash.get replacement)"""
    keys = path.split('.')
    current = obj
    for key in keys:
        if isinstance(current, dict):
            if key.isdigit():
                key = int(key)
            current = current.get(key)
        elif isinstance(current, list) and key.isdigit():
            idx = int(key)
            current = current[idx] if 0 <= idx < len(current) else None
        else:
            return None
        if current is None:
            return None
    return current

def set_nested_value(obj, path, value):
    """Set nested value in dict using dot notation (pydash.set_ replacement)"""
    keys = path.split('.')
    current = obj
    for key in keys[:-1]:
        if key.isdigit():
            key = int(key)
        if key not in current:
            current[key] = {}
        current = current[key]
    
    final_key = keys[-1]
    if final_key.isdigit():
        final_key = int(final_key)
    current[final_key] = value

class SpotifyWebSocket:
    """Spotify WebSocket implementation using external AsyncWebsocketClient"""
    
    def __init__(self, spotify_client):
        self.client = spotify_client
        self.previous_payload = {}
        self.running = False
        self.ws_client = None
        self._connection_attempts = 0
        self._max_connection_attempts = 3
        
    def _check_differences(self, keys, new_obj):
        """Check differences between previous and current object"""
        EVENTS_MAPPING = {
            "is_playing": "is_playing",
            "volume": "volume",
            "options": "playback_options",
            "uri": "track",
        }
        
        for key in keys:
            new_value = get_nested_value(new_obj, key)
            old_value = get_nested_value(self.previous_payload, key)
            
            if old_value != new_value:
                if key.split('.')[-1] == 'is_paused':
                    emit_value = 'pause' if new_value else 'resume'
                    self.client.event_emitter.emit(emit_value)
                else:
                    key_suffix = key.split('.')[-1]
                    event_name = EVENTS_MAPPING[key_suffix]

                    # Send full payload object for all other events
                    track_object = get_nested_value(new_obj, "payloads.0")
                    self.client.event_emitter.emit(event_name, track_object)

                    # Special case: when track URI changes, emit the entire object
                    #if key == "payloads.0.cluster.player_state.track.uri" and event_name == "track":
                        # Get the full track object from the correct path
                    #    track_object = get_nested_value(new_obj, "payloads.0.cluster.player_state")
                    #    self.client.event_emitter.emit(event_name, track_object)
                    #else:
                    #    self.client.event_emitter.emit(event_name, new_value)

                    #field_name = key.split('.')[-1]
                    #if field_name in EVENTS_MAPPING:
                    #    self.client.event_emitter.emit(
                    #        EVENTS_MAPPING[field_name],
                    #        new_value
                    #    )
                set_nested_value(self.previous_payload, key, new_value)
    
    def _handle_device_state_changed(self, message):
        """Handle DEVICE_STATE_CHANGED event"""
        keys = [
            "payloads.0.cluster.player_state.is_playing",
            "payloads.0.cluster.player_state.is_paused",
            "payloads.0.cluster.player_state.options",
            "payloads.0.cluster.player_state.track.uri"
        ]
        self._check_differences(keys, message)
    
    def _handle_device_volume_changed(self, message):
        """Handle DEVICE_VOLUME_CHANGED event"""
        active_device_path = "payloads.0.cluster.active_device_id"
        active_device = get_nested_value(message, active_device_path)
        
        if active_device:
            keys = [f"payloads.0.cluster.devices.{active_device}.volume"]
            self._check_differences(keys, message)
    
    def _handle_message(self, message):
        """Handle incoming WebSocket message"""
        handlers = {
            "DEVICE_STATE_CHANGED": self._handle_device_state_changed,
            "DEVICE_VOLUME_CHANGED": self._handle_device_volume_changed,
            "DEVICES_DISAPPEARED": self._handle_device_volume_changed,
        }
        
        payloads = message.get('payloads')
        if not payloads:
            return
        
        reason = payloads[0].get('update_reason')
        if not reason:
            return
        
        if reason in handlers:
            handlers[reason](message)
    
    def on_message(self, message):
        """Handle WebSocket message"""
        try:
            #print(f"WebSocket received message: {message}")
            msg = json.loads(message)
            #print(f"WebSocket message parsed: {msg}")
            
            headers = msg.get('headers', {})
            
            if 'Spotify-Connection-Id' in headers:
                #print(f"Got connection ID: {headers['Spotify-Connection-Id']}")
                self.client._set_connection_id(headers['Spotify-Connection-Id'])
                return
            
            if msg.get('type') == 'message':
                self._handle_message(msg)
        except Exception as e:
            print(f"Error handling WebSocket message: {e}")
    
    def on_close(self):
        """Handle WebSocket close"""
        print(f"WebSocket closed")
        self.running = False
    
    def on_error(self, error):
        """Handle WebSocket error"""
        error_str = str(error)
        if "settimeout" not in error_str:
            print(f"WebSocket error: {error}")
    
    async def start(self):
        """Start WebSocket connection with asyncio"""
        self.running = True
        url = f"{DEALER_WS_URL}?access_token={self.client.access_token}"
        print(f"WebSocket URL: {url}")
        
        while self.running and self._connection_attempts < self._max_connection_attempts:
            self._connection_attempts += 1
            self.ws_client = AsyncWebsocketClient()
            
            try:
                print(f"WebSocket connection attempt {self._connection_attempts}/{self._max_connection_attempts}")
                
                token_preview = self.client.access_token[:50] + "..." if len(self.client.access_token) > 50 else self.client.access_token
                #print(f"Using access token: {token_preview}")
                
                await self.ws_client.handshake(url)
                print("WebSocket connected successfully")
                
                # Reset connection attempts on successful connection
                self._connection_attempts = 0
                
                # Main message loop
                while self.running and await self.ws_client.open():
                    try:
                        message = await self.ws_client.recv()
                        if message is not None:
                            self.on_message(message)
                        else:
                            print("WebSocket recv returned None, connection may be closed")
                            break
                    except Exception as e:
                        print(f"Error receiving WebSocket message: {e}")
                        self.on_error(e)
                        break
                        
            except Exception as e:
                print(f"WebSocket connection error (attempt {self._connection_attempts}): {e}")
                
                if "401" in str(e) or "Unauthorized" in str(e):
                    print("WebSocket authentication failed!")
                    
                    if self._connection_attempts == 1:
                        print("Attempting to refresh access token...")
                        try:
                            self.client.access_token = self.client._get_access_token()
                            url = f"{DEALER_WS_URL}?access_token={self.client.access_token}"
                            print("Access token refreshed, will retry...")
                        except Exception as token_error:
                            print(f"Failed to refresh token: {token_error}")
                
                self.on_error(e)
                
                if self.running and self._connection_attempts < self._max_connection_attempts:
                    print(f"Retrying WebSocket connection in 2 seconds...")
                    await asyncio.sleep(2)
                    
            finally:
                if self.ws_client:
                    await self.ws_client.close()
                    
        if self._connection_attempts >= self._max_connection_attempts:
            print(f"WebSocket failed after {self._max_connection_attempts} attempts")
        
        self.on_close()

class SpotifyWebsocketClient:
    """Main Spotify client with asyncio support"""
    
    def __init__(self, cookie_file=None, verbose=False):
        self.verbose = verbose
        self.device = DEFAULT_DEVICE.copy()
        self.user_devices = []
        self.connection_id = None
        self.access_token = None
        self.event_emitter = SimpleEventEmitter()
        self._connection_id_ready = False
        
        # Load cookies
        if cookie_file:
            self.cookies = self._load_cookies_from_file(cookie_file)
        else:
            raise Exception("You must provide a file containing your browser session cookies.")
        
        # Get access token
        self.access_token = self._get_access_token()
        
        # Create WebSocket (don't start yet, will be started in async context)
        self.ws = SpotifyWebSocket(self)
    
    def _load_cookies_from_file(self, cookie_file):
        """Load cookies from file"""
        try:
            with open(cookie_file, 'r') as f:
                parsed_cookies = json.load(f)
            
            if isinstance(parsed_cookies, list):
                formatted_cookies = {}
                for cookie in parsed_cookies:
                    formatted_cookies[cookie["name"]] = cookie["value"]
                parsed_cookies = formatted_cookies
            
            return parsed_cookies
        except Exception as e:
            print(f"Error loading cookies: {e}")
            raise
    
    def _get_cookie_header(self):
        """Build cookie header string from self.cookies"""
        return '; '.join([f"{k}={v}" for k, v in self.cookies.items()])
    
    def _get_access_token(self):
        """Get Spotify access token using urequests"""
        totp, _ = generate_totp()
        print(f"Generated TOTP: {totp}")
        
        # Build params exactly like the original
        params = {
            "reason": "init",
            "productType": "web-player",
            "totp": totp,
            "totpVer": 5,
            "ts": int(time.time() * 1000)
        }
        
        # Build URL with parameters
        param_string = '&'.join([f"{k}={v}" for k, v in params.items()])
        url = f"{GET_SPOTIFY_TOKEN}?{param_string}"
        print(f"Making request to: {url}")
        
        # Headers that should work
        headers = {
            'User-Agent': 'python-requests/2.31.0',
            'Accept': '*/*',
            'Cookie': self._get_cookie_header()
        }
        
        # Make request using urequests
        try:
            response = requests.get(url, headers=headers)
            #print(f"Response status: {response.status_code}")
            response_text = response.text
            #print(f"Response text: {response_text}")
            response.close()
            
            # Parse the response
            if response.status_code == 200:
                result = json.loads(response_text)
                #print(f"Parsed JSON: {result}")
                
                if 'accessToken' in result:
                    print("SUCCESS: Got access token")
                    return result["accessToken"]
                else:
                    if 'error' in result:
                        print(f"API Error: {result['error']}")
                        raise Exception(f"Token error: {result['error']}")
                    else:
                        raise Exception(f"Unexpected response: {result}")
            else:
                raise Exception(f"HTTP {response.status_code}: {response_text}")
                    
        except Exception as e:
            print(f"ERROR getting access token: {e}")
            raise
    
    def _set_connection_id(self, connection_id):
        """Set connection ID from WebSocket"""
        self.connection_id = connection_id
        self._connection_id_ready = True
    
    async def _wait_for_connection_id(self, timeout=30):
        """Wait for connection ID to be ready with async support"""
        print("Waiting for connection ID from WebSocket...")
        start_time = time.time()
        
        while not self._connection_id_ready and (time.time() - start_time) < timeout:
            await asyncio.sleep(0.1)
        
        if not self._connection_id_ready:
            print(f"ERROR: No connection ID received after {timeout} seconds")
            raise Exception("Timeout waiting for connection ID")
        
        #print(f"Connection ID received: {self.connection_id}")
    
    def _request(self, method, url, headers=None, data=None, json_data=None, retry=True):
        """Make HTTP request with automatic token refresh using urequests"""
        if headers is None:
            headers = {}
        
        # Add authorization header
        headers['Authorization'] = f'Bearer {self.access_token}'
        
        # Add cookies
        headers['Cookie'] = self._get_cookie_header()
        
        # Debug logging
        print(f"Making {method} request to: {url}")
        #if self.verbose:
            #print(f"Headers: {headers}")
            #if data:
            #    print(f"Data: {data[:500]}..." if len(str(data)) > 500 else f"Data: {data}")
        
        try:
            # Make request using urequests
            if method == 'GET':
                response = requests.get(url, headers=headers)
            elif method == 'POST':
                if json_data is not None:
                    headers['Content-Type'] = 'application/json'
                    data = json.dumps(json_data)
                response = requests.post(url, headers=headers, data=data)
            elif method == 'PUT':
                if json_data is not None:
                    headers['Content-Type'] = 'application/json'
                    data = json.dumps(json_data)
                response = requests.put(url, headers=headers, data=data)
            else:
                raise Exception(f"Unsupported method: {method}")
            
            # Log response details
            #print(f"Response status: {response.status_code}")
            #if response.status_code != 200:
            #    print(f"Response text: {response.text}")
            
            if response.status_code == 401 and retry:
                #print("401 Unauthorized - attempting to refresh token")
                response.close()
                self.access_token = self._get_access_token()
                return self._request(method, url, headers, data, json_data, retry=False)
            
            return response
        except Exception as e:
            print(f"Request error: {e}")
            raise
    
    async def _subscribe_to_activity(self):
        """Subscribe to Spotify activity"""
        if self.verbose:
            print("Subscribing to activities")
        
        await self._wait_for_connection_id()

        url = f"{SUBSCRIBE_ACTIVITY}?connection_id={self.connection_id}"
        headers = {
            "referer": "https://open.spotify.com/",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.4664.110 Safari/537.36"
        }

        try:
            response = self._request('PUT', url, headers=headers, data='{}')
            print(f"Subscribe to activity response: {response.status_code}")
            if response.status_code in [200, 400, 410]:
                print("Activity subscription successful")
                return
            else:
                print(f"Subscribe to activity failed: {response.text}")
        except Exception as e:
            print(f"Error subscribing to activity: {e}")
            # Don't raise - continue without activity subscription
    
    async def _register_fake_device(self):
        """Register fake device"""
        if self.verbose:
            print("Registering fake device to listen to events")

        await self._wait_for_connection_id()

        data = {
            "client_version": "harmony:4.21.0-a4bc573",
            "connection_id": self.connection_id,
            "device": self.device,
            "outro_endcontent_snooping": False,
            "volume": 65535
        }

        #print(f"Registering device with data: {data}")

        try:
            response = self._request('POST', REGISTER_DEVICE, json_data=data)
            print(f"Register device response: {response.status_code}")
            if response.status_code != 200:
                print(f"Register device failed: {response.text}")
                print("Fake device registration failed")
            else:
                print("Device registered successfully")
        except Exception as e:
            print(f"Error registering device: {e}")
            print("Continuing without fake device registration...")
    
    async def _connect_state(self):
        """Connect to Spotify state"""
        if self.verbose:
            print("Connecting device to Spotify state")
        
        default_options = {
            "device": {
                "device_info": {
                    "capabilities": {
                        "can_be_player": False,
                        "hidden": False,
                        "volume_steps": 64,
                        "supported_types": [
                            "audio/track",
                            "audio/episode",
                            "video/episode",
                            "mixed/episode"
                        ],
                        "needs_full_player_state": True,
                        "command_acks": True,
                        "is_controllable": True,
                        "supports_command_request": True,
                        "supports_set_options_command": True,
                    }
                }
            },
            "member_type": "CONNECT_STATE",
        }
        
        url = f"{CONNECT_STATE}hobs_{self.device['device_id']}"
        headers = {"x-spotify-connection-id": self.connection_id}
        
        try:
            response = self._request('PUT', url, headers=headers, json_data=default_options)
            print(f"Connect state response: {response.status_code}")
            if response.status_code != 200:
                print(f"Connect state failed: {response.text}")
                return
            
            devices_data = response.json()
            #print(f"Connect state response data: {devices_data}")
            
            # Process devices
            for dev_id, dev_info in devices_data.get("devices", {}).items():
                device_dict = {"id": dev_id, "can_play": dev_info.get('can_play', False)}
                device_dict.update(dev_info)
                self.user_devices.append(device_dict)
                #print(f"Added device: {device_dict}")
                
            print(f"Total devices found: {len(self.user_devices)}")
        except Exception as e:
            print(f"Error connecting to state: {e}")
            # Don't raise here, continue without devices
    
    async def start(self):
        """Start the client and initialize WebSocket connection"""
        # Start WebSocket
        ws_task = asyncio.create_task(self.ws.start())
        
        # Wait a bit for WebSocket to connect
        print("Waiting for WebSocket to establish...")
        await asyncio.sleep(2)
        
        print("Starting Spotify client initialization...")
        try:
            await self._subscribe_to_activity()
            print("Activity subscription completed")
        except Exception as e:
            print(f"Failed to subscribe to activity: {e}")
            # Continue initialization even if this fails
        
        try:
            await self._register_fake_device()
            print("Device registration completed")
        except Exception as e:
            print(f"Failed to register device: {e}")
            # Continue initialization even if this fails
        
        try:
            await self._connect_state()
            print("State connection completed")
        except Exception as e:
            print(f"Failed to connect state: {e}")
            # Continue initialization even if this fails
        
        print(f"Initialization complete. Found {len(self.user_devices)} devices.")
        print("Client ready!")
        
        return ws_task
    
    def on(self, event, callback=None):
        """Register event listener using decorator syntax"""
        if callback is None:
            # Decorator usage: @client.on('event')
            def wrapper(func):
                self.event_emitter.on(event, func)
                return func
            return wrapper
        else:
            # Direct usage: client.on('event', callback)
            self.event_emitter.on(event, callback)
            return callback
    
    def add_listener(self, event, callback):
        """Add event listener"""
        self.event_emitter.on(event, callback)
    
    def get_available_devices(self):
        """Get available devices"""
        return self.user_devices
    
    def send_command(self, command, target_device, *args):
        """Send command to device"""
        if command in ["pause", "resume", "skip_next", "skip_prev"]:
            if len(args) != 0:
                raise Exception("This command takes no args")
        else:
            if len(args) != 1:
                raise Exception("You must provide a value")
        
        if not target_device:
            raise Exception("You must provide a valid target_device")
        
        command_arg = args[0] if len(args) > 0 else None
        
        available_commands = {
            "pause": {"command": {"endpoint": "pause"}},
            "resume": {"command": {"endpoint": "resume"}},
            "skip_next": {"command": {"endpoint": "skip_next"}},
            "skip_prev": {"command": {"endpoint": "skip_prev"}},
            "seek_to": {"command": {"endpoint": "seek_to", "value": command_arg}},
            "volume": {"volume": command_arg},
        }
        
        if command not in available_commands:
            raise Exception(f"Unknown command: {command}")
        
        command_payload = available_commands[command]
        
        if command == "volume":
            url = f"https://gew1-spclient.spotify.com/connect-state/v1/connect/volume/from/{self.device['device_id']}/to/{target_device}"
            response = self._request('PUT', url, json_data=command_payload)
        else:
            url = f"https://gew1-spclient.spotify.com/connect-state/v1/player/command/from/{self.device['device_id']}/to/{target_device}"
            response = self._request('POST', url, json_data=command_payload)

# Example usage with asyncio
async def main():
    # Initialize client
    client = SpotifyWebsocketClient(cookie_file="../../cookies.json", verbose=False)

    def format_time(timestamp):
        t = time.localtime(timestamp)
        return "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format( t[0], t[1], t[2], t[3], t[4], t[5] )

    # Add event listeners
    @client.on('track')
    def on_track_change(player_state):
        print(f"Track changed => [{format_time(time.time())}] {player_state.get('track').get('uri')} - {player_state.get('track').get('metadata').get('title')}")

    @client.on('is_playing')
    def on_playback_state(is_playing):
        print(f"Playback state: {'Playing' if is_playing else 'Paused'}")
    
    @client.on('volume')
    def on_volume_change(volume):
        print("Volume changed => ", round(volume / 65535 * 100), "%")
    
    # Start the client and get the WebSocket task
    ws_task = await client.start()
    
    # Wait and show available devices
    await asyncio.sleep(3)
    devices = client.get_available_devices()
    print(f"Available devices: {devices}")
    
    # Keep running - WebSocket task will handle the connection
    try:
        await ws_task
    except KeyboardInterrupt:
        print("Shutting down...")
    finally:
        client.ws.running = False

# Example usage
if __name__ == "__main__":
    asyncio.run(main())