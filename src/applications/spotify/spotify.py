import gc
import time
import jpegdec
import pngdec
import uasyncio as asyncio
import urequests as requests

from touch import Button

from applications.spotify.spotify_websocket_client import SpotifyWebsocketClient
from applications.spotify.spotify_http_client import Session, SpotifyWebApiClient
from base import BaseApp
import secrets

class TimingCollector:
    """Collects and reports timing information for performance analysis"""
    def __init__(self):
        self.times = {}
        self.counts = {}
        self.last_report = time.ticks_ms()
        
    def start_timer(self, name):
        """Start timing an operation using millisecond precision"""
        return time.ticks_ms()
    
    def end_timer(self, name, start_time):
        """End timing and record the result"""
        duration_ms = time.ticks_diff(time.ticks_ms(), start_time)
        duration = duration_ms / 1000.0  # Convert back to seconds for consistency
        
        if name not in self.times:
            self.times[name] = []
            self.counts[name] = 0
        self.times[name].append(duration)
        self.counts[name] += 1
        
        # Keep only last 10 measurements to avoid memory bloat
        if len(self.times[name]) > 10:
            self.times[name] = self.times[name][-10:]
    
    def get_average(self, name):
        """Get average time for an operation"""
        if name not in self.times or not self.times[name]:
            return 0
        return sum(self.times[name]) / len(self.times[name])
    
    def get_min_max(self, name):
        """Get min and max times for an operation"""
        if name not in self.times or not self.times[name]:
            return 0, 0
        return min(self.times[name]), max(self.times[name])
    
    def report(self):
        """Print timing report with proper precision"""
        current_time = time.ticks_ms()
        if time.ticks_diff(current_time, self.last_report) < 10000:  # Report every 10 seconds
            return
            
        print("\n=== TIMING REPORT ===")
        print(f"{'Operation':<30} | {'Avg':>8} | {'Min':>8} | {'Max':>8} | {'Count':>5}")
        print("-" * 70)
        
        for name in sorted(self.times.keys()):
            avg_time = self.get_average(name)
            min_time, max_time = self.get_min_max(name)
            count = self.counts[name]
            
            # Only show operations that have been called and take meaningful time
            if count > 0 and avg_time > 0.001:  # Show only > 1ms operations
                print(f"{name:<30} | {avg_time*1000:7.1f}ms | {min_time*1000:7.1f}ms | {max_time*1000:7.1f}ms | {count:5d}")
        
        print("=" * 70)
        self.last_report = current_time

# Global timing collector
timing = TimingCollector()

def timed(operation_name):
    """Decorator to time function calls - simplified for MicroPython"""
    def decorator(func):
        def wrapper(*args, **kwargs):
            start_time = timing.start_timer(operation_name)
            try:
                result = func(*args, **kwargs)
                timing.end_timer(operation_name, start_time)
                return result
            except Exception as e:
                timing.end_timer(operation_name, start_time)
                raise e
        return wrapper
    return decorator

def timed_async(operation_name):
    """Decorator for async functions - separate from sync version"""
    def decorator(func):
        async def wrapper(*args, **kwargs):
            start_time = timing.start_timer(operation_name)
            try:
                result = await func(*args, **kwargs)
                timing.end_timer(operation_name, start_time)
                return result
            except Exception as e:
                timing.end_timer(operation_name, start_time)
                raise e
        return wrapper
    return decorator

class State:
    """Tracks the current state of the Spotify app including playback and UI controls."""
    def __init__(self):
        self.toggle_leds = True
        self.is_playing = False
        self.repeat = False
        self.shuffle = False
        self.track = None
        self.show_controls = True
        self.exit = False

        self.latest_fetch = None
    
    def copy(self):
        state = State()
        state.toggle_leds = self.toggle_leds
        state.is_playing = self.is_playing
        state.repeat = self.repeat
        state.shuffle = self.shuffle
        state.show_controls = self.show_controls
        state.exit = self.exit
        state.track = self.track #{'id': self.track['id']} if self.track else None # only care about track id
        return state
    
    def __eq__(self, other):
        if not isinstance(other, State) or other is None:
            return False
        return (
            self.toggle_leds == other.toggle_leds and
            self.is_playing == other.is_playing and
            self.repeat == other.repeat and
            self.shuffle == other.shuffle and
            self.show_controls == other.show_controls and
            self.exit == other.exit and
            #(self.track or {}).get('id') == (other.track or {}).get('id')
            self.track == other.track
        )

class ControlButton():
    """Represents a control button with an icon and touch area."""
    def __init__(self, display, name, icons, bounds, on_press=None, update=None):
        self.name = name
        self.enabled = False
        self.icon = icons[0] if icons else None
        self.pngs = {}
        if icons:
            for icon in icons:
                png = pngdec.PNG(display)
                png.open_file("applications/spotify/icons/" + icon)
                self.pngs[icon] = png

        self.button = Button(*bounds)
        self.on_press = on_press
        self.update = update

    def is_pressed(self, state):
        """Checks if the button is enabled and currently pressed."""
        return self.enabled and self.button.is_pressed()
    
    @timed("button_draw")
    def draw(self, state):
        """Draws the button icon if enabled."""
        if self.enabled and self.icon:
            self.draw_icon()

    def draw_icon(self):
        """Renders the button's icon centered inside its bounds."""
        png = self.pngs[self.icon]
        x, y, width, height = self.button.bounds
        png_width, png_height = png.get_width(), png.get_height()
        x_offset = (width-png_width)//2
        y_offset = (height-png_height)//2

        png.decode(x+x_offset, y+y_offset)

class ImageCache:
    """Aggressive image caching to avoid repeated downloads"""
    def __init__(self, max_size=5):
        self.cache = {}
        self.urls = []  # Track insertion order
        self.max_size = max_size

    def get(self, url):
        return self.cache.get(url)

    def set(self, url, image):
        if url in self.cache:
            return

        # Remove oldest if cache full
        if len(self.cache) >= self.max_size:
            oldest_url = self.urls.pop(0)
            del self.cache[oldest_url]

        self.cache[url] = image
        self.urls.append(url)


def ntpsync():
    """Sync time with NTP server, retrying until successful."""
    print("Syncing time with NTP...")

    # Maximum number of retries before giving up
    max_retries = 5
    # Initial retry delay in seconds
    retry_delay = 1
    # Whether sync was successful
    sync_success = False

    for attempt in range(1, max_retries + 1):
        try:
            import ntptime
            # Set NTP server - use pool.ntp.org as fallback if time.nist.gov fails
            ntptime.host = "time.nist.gov" if attempt <= 2 else "pool.ntp.org"
            ntptime.settime()
            print(f"Time after NTP sync: {time.localtime()}")
            sync_success = True
            break
        except Exception as e:
            print(f"NTP sync failed (attempt {attempt}/{max_retries}): {e}")
            print(f"Retrying in {retry_delay} seconds...")
            time.sleep(retry_delay)
            # Exponential backoff with a cap
            retry_delay = min(retry_delay * 2, 10)

    # If we've exhausted all retries, keep trying indefinitely with a longer delay
    if not sync_success:
        print("All initial retries failed. Continuing with extended retries...")
        retry_delay = 15
        attempt = max_retries

        while not sync_success:
            try:
                import ntptime
                # Alternate between servers
                ntptime.host = "time.nist.gov" if attempt % 2 == 0 else "pool.ntp.org"
                ntptime.settime()
                print(f"Time after NTP sync: {time.localtime()}")
                sync_success = True
                break
            except Exception as e:
                attempt += 1
                print(f"NTP sync failed (extended attempt {attempt}): {e}")
                print(f"Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
                # Keep a reasonable maximum delay
                retry_delay = min(retry_delay * 1.5, 60)

    print("NTP sync completed successfully.")
    return sync_success


class Spotify(BaseApp):
    """Main Spotify app managing playback controls, track display, and UI interactions."""
    def __init__(self):
        super().__init__(ambient_light=True, full_res=True, layers=2)

        self.track_cache = None

        self.display.set_layer(0)
        icon = pngdec.PNG(self.display)
        icon.open_file("applications/spotify/icon.png")
        icon.decode(self.center_x - icon.get_width()//2, self.center_y - icon.get_height()//2 - 20)
        self.presto.update()

        self.display.set_font("sans")
        self.display.set_layer(1)
        self.display_text("Connecting to WIFI", (90, self.height - 80), thickness=2)
        self.presto.update()

        self.presto.connect()
        while not self.presto.wifi.isconnected():
            self.clear(1)
            self.display_text("Failed to connect to WIFI", (40, self.height - 80), thickness=2)
            time.sleep(2)

        self.clear(1)
        self.display.set_layer(1)
        self.display_text("Syncing NTP Time", (90, self.height - 80), thickness=2)
        self.presto.update()
        ntpsync()

        self.clear(1)
        self.display_text("Instantiating Spotify Client", (35, self.height - 80), thickness=2)
        self.spotify_client = self.get_spotify_client()
        self.spotify_websocket_client = SpotifyWebsocketClient(cookie_file="../../cookies.json", verbose=False)
        self.setup_websocket_integration()
        self.clear(1)
        self.presto.update()

        # JPEG decoder
        self.j = jpegdec.JPEG(self.display)

        self.state = State()
        self.setup_buttons()
        
        # Add image cache to prevent repeated downloads
        self.image_cache = ImageCache(max_size=5)
        self._gc_counter = 0  # Optimize garbage collection frequency
        
        # Initial UI setup
        print("Setting up initial UI...")
        self.clear(1)
        # Update all buttons to initial state (particularly the invisible "Toggle Controls" button)
        for button in self.buttons:
            button.update(self.state, button)
        self.presto.update()
        print("Initial UI ready")

    def display_text(self, text, position, color=65535, scale=1, thickness=None):
        if thickness:
            self.display.set_thickness(2)
        x,y = position
        self.display.set_pen(color)
        self.display.text(text, x, y, scale=scale)
        self.presto.update()

    def get_spotify_client(self):
        if not hasattr(secrets, 'SPOTIFY_CREDENTIALS') or not secrets.SPOTIFY_CREDENTIALS:
            while True:
                self.clear(1)
                self.display.set_pen(self.colors.WHITE)
                self.display.text("Spotify credentials not found", 40, self.height - 80, scale=.9)
                self.presto.update()
                time.sleep(2)

        session = Session(secrets.SPOTIFY_CREDENTIALS)
        return SpotifyWebApiClient(session)
        
    def setup_buttons(self):
        """Initializes control buttons and their behavior."""
        # --- Shared update functions ---
        def update_show_controls(state, button):
            button.enabled = state.show_controls

        def update_always_enabled(state, button):
            button.enabled = True

        def update_play_pause(state, button):
            button.enabled = state.show_controls
            button.icon = "pause.png" if state.is_playing else "play.png"

        def update_shuffle(state, button):
            button.enabled = state.show_controls
            button.icon = "shuffle_on.png" if state.shuffle else "shuffle_off.png"

        def update_repeat(state, button):
            button.enabled = state.show_controls
            button.icon = "repeat_on.png" if state.repeat else "repeat_off.png"

        def update_light(state, button):
            button.enabled = state.show_controls
            button.icon = "light_on.png" if state.toggle_leds else "light_off.png"

        # --- On-press handlers with timing ---
        @timed("button_exit")
        def exit_app(self):
            self.state.exit = True

        @timed("button_toggle_controls")
        def toggle_controls(self):
            self.state.show_controls = not self.state.show_controls

        @timed("button_play_pause")
        def play_pause(self):
            try:
                if self.state.is_playing:
                    self.spotify_client.pause()
                else:
                    self.spotify_client.play()
                self.state.is_playing = not self.state.is_playing
            except Exception as e:
                print(f"Play/pause error: {e}")

        @timed("button_next_track")
        def next_track(self):
            try:
                self.spotify_client.next()
                self.state.latest_fetch = None
            except Exception as e:
                print(f"Next track error: {e}")

        @timed("button_previous_track")
        def previous_track(self):
            try:
                self.spotify_client.previous()
                self.state.latest_fetch = None
            except Exception as e:
                print(f"Previous track error: {e}")

        @timed("button_toggle_shuffle")
        def toggle_shuffle(self):
            try:
                self.spotify_client.toggle_shuffle(not self.state.shuffle)
                self.state.shuffle = not self.state.shuffle
            except Exception as e:
                print(f"Shuffle error: {e}")

        @timed("button_toggle_repeat")
        def toggle_repeat(self):
            try:
                self.spotify_client.toggle_repeat(not self.state.repeat)
                self.state.repeat = not self.state.repeat
            except Exception as e:
                print(f"Repeat error: {e}")

        @timed("button_toggle_lights")
        def toggle_lights(self):
            try:
                self.toggle_leds(not self.state.toggle_leds)
                self.state.toggle_leds = not self.state.toggle_leds
            except Exception as e:
                print(f"Lights error: {e}")

        # --- Button configurations ---
        buttons_config = [
            ("Exit", ["exit.png"], (0, 0, 80, 80), exit_app, update_show_controls),
            ("Next", ["next.png"], (self.center_x + 60, self.height - 100, 80, 100), next_track, update_show_controls),
            ("Previous", ["previous.png"], (self.center_x - 140, self.height - 100, 80, 100), previous_track, update_show_controls),
            ("Play", ["play.png", "pause.png"], (self.center_x - 50, self.height - 100, 80, 100), play_pause, update_play_pause),
            ("Toggle Shuffle", ["shuffle_on.png", "shuffle_off.png"], (self.center_x - 230, self.height - 100, 80, 100), toggle_shuffle, update_shuffle),
            ("Toggle Repeat", ["repeat_on.png", "repeat_off.png"], (self.center_x + 150, self.height - 100, 80, 100), toggle_repeat, update_repeat),
            ("Toggle Light", ["light_on.png", "light_off.png"], (self.width - 100, 0, 100, 80), toggle_lights, update_light),
            ("Toggle Controls", None, (0, 0, self.width, self.height), toggle_controls, update_always_enabled),
        ]

        # --- Create ControlButton instances ---
        self.buttons = [
            ControlButton(self.display, name, icons, bounds, on_press, update)
            for name, icons, bounds, on_press, update in buttons_config
        ]

    def run(self):
        """Starts the app's event loops."""
        loop = asyncio.get_event_loop()
        try:
            loop.create_task(self.touch_handler_loop())
            loop.create_task(self.display_loop())
            loop.create_task(self.websocket_loop())  # Separate task for WebSocket events
            loop.create_task(self.fetch_loop())  # Separate task for API calls
            loop.create_task(self.image_loop())  # Separate task for image loading
            #loop.create_task(self.timing_report_loop())
            loop.run_forever()
        except KeyboardInterrupt:
            print("App interrupted by user")
        finally:
            self.clear()
            gc.collect()

    async def get_album_cover_cached_async(self, img_url):
        """Get album cover with aggressive caching - async version"""
        
        # Check cache first
        cached_img = self.image_cache.get(img_url)
        if cached_img:
            print(f"Cache HIT for {img_url} - image size: {len(cached_img)} bytes")
            return cached_img
        
        # Download if not cached - yield during download
        print(f"Cache MISS - Downloading {img_url}")
        
        # Yield before starting download
        await asyncio.sleep_ms(1)
        
        # Download in background (this still blocks, but we can yield around it)
        img = get_album_cover(img_url)
        
        # Yield after download
        await asyncio.sleep_ms(1)
        
        if img:
            print(f"Downloaded image size: {len(img)} bytes")
            self.image_cache.set(img_url, img)
            return img
        else:
            print("Failed to download image")
            return None

    async def image_loop(self):
        """Background task for image loading to prevent UI blocking"""
        last_processed_track_uri = None

        while not self.state.exit:
            current_track_uri = (self.state.track or {}).get('uri') or self.state.track.get('metadata', {}).get('uri')

            # Check if we have a new track that needs an image
            if current_track_uri and current_track_uri != last_processed_track_uri and self.state.track:

                # Get info entry
                track_info = self.track_cache.get(current_track_uri)
                if track_info:
                    print(f"Image loop: Processing {track_info.get('song_name')}")
                    last_processed_track_uri = current_track_uri

                    img = await self.get_album_cover_cached_async(track_info.get('background_image'))

                    if img:
                        print(f"Image loop: Found image in cache for {track_info.get('song_name')}")
                        print(f"Image loop: Image size: {len(img)} bytes")
                        await self._show_image_async(img)
            
            await asyncio.sleep(0.1)  # Check for new tracks every 100ms

    async def _show_image_async(self, img):
        """Async image display with yielding"""
        if not img:
            return
            
        try:
            decode_start = timing.start_timer("image_decode_async")
            
            # Yield before heavy operations
            await asyncio.sleep_ms(1)
            
            self.j.open_RAM(memoryview(img))
            img_width, img_height = self.j.get_width(), self.j.get_height()
            img_x, img_y = (self.width - img_width) // 2, (self.height - img_height) // 2

            # Yield during decode
            await asyncio.sleep_ms(1)
            
            # Clear layer 0 and draw the image
            self.display.set_layer(0)
            self.clear(0)
            self.j.decode(img_x, img_y, jpegdec.JPEG_SCALE_FULL, dither=True)
            
            # Yield before update
            await asyncio.sleep_ms(1)
            
            self.presto.update()
            timing.end_timer("image_decode_async", decode_start)
            print(f"Async image displayed at ({img_x}, {img_y}) size {img_width}x{img_height}")

        except OSError as e:
            print(f"Failed to load image async: {e}")
            timing.end_timer("image_decode_async", decode_start)

    def setup_websocket_integration(self):
        """Set up WebSocket events to optionally enhance the main app state updates"""
        
        # Add event handlers to get real-time updates via WebSocket
        @self.spotify_websocket_client.on('track')
        def on_websocket_track_change(payload):
            print(f"WebSocket detected track change: {payload.get('cluster').get('player_state').get('track', {}).get('metadata', {}).get('title', 'Unknown')}")
            self.state.track = payload.get('cluster').get('player_state').get('track')
            self.state.is_playing = payload.get('cluster').get('player_state').get('is_playing')
            self.state.shuffle = payload.get('cluster').get('player_state').get('options').get('shuffling_context')
            self.state.repeat = payload.get('cluster').get('player_state').get('options').get('repeating_track')
            self.spotify_client.session.device_id = payload.get('cluster').get('active_device_id')
            self.state.latest_fetch = time.time()

            # Fetch and cache tracks/images in the background
            asyncio.create_task(self.fetch_and_cache_tracks(payload.get('cluster')))

            print(f"Got to end of websocket track change")
        
        @self.spotify_websocket_client.on('is_playing')
        def on_websocket_playback_state(is_playing):
            print(f"WebSocket detected playback state: {'Playing' if is_playing else 'Paused'}")
            # Update state immediately for responsiveness
            self.state.is_playing = is_playing
        
        @self.spotify_websocket_client.on('pause')
        def on_websocket_pause():
            print("WebSocket: Track paused")
            self.state.is_playing = False
        
        @self.spotify_websocket_client.on('resume')
        def on_websocket_resume():
            print("WebSocket: Track resumed") 
            self.state.is_playing = True

    async def websocket_loop(self):
        """Manage WebSocket client lifecycle - separate from main app logic"""
        print("Starting WebSocket client for real-time updates...")
        try:
            # Start WebSocket client - it will handle its own events and update our state
            await self.spotify_websocket_client.start()
        except Exception as e:
            print(f"WebSocket error: {e}")
            print("Continuing without WebSocket - HTTP polling will still work")

    @timed_async("track_fetcher_new")
    async def fetch_and_cache_tracks(self, cluster):
        """Fetches and caches track information from the Spotify API."""
        print(f"[MOOO] Fetching and caching tracks")

        # Extract track URI's from cluster data structure (spotify:track:xxxx)
        uris = [cluster.get('player_state').get('track').get('uri')]

        # Current track
        print(f"[MOOO] Current track: {cluster.get('player_state').get('track').get('uri')}")

        # Previous track (only the first one)
        if cluster.get('player_state').get('prev_tracks') and len(cluster.get('player_state').get('prev_tracks')) > 0:
            prev_track = cluster.get('player_state').get('prev_tracks')[0]
            if 'uri' in prev_track:
                uris.append(prev_track.get('uri'))
                print(f"[MOOO] Previous track: {prev_track.get('uri')}")

        # Next tracks (up to 3)
        if cluster.get('player_state').get('next_tracks'):
            for i, next_track in enumerate(cluster.get('player_state').get('next_tracks')):
                if i >= 3:  # Limit to 3 next tracks
                    break
                if 'uri' in next_track:
                    uris.append(next_track.get('uri'))
                    print(f"[MOOO] Next track: {next_track.get('uri')}")

        # Remove duplicates and filter out None or empty IDs
        uris = [track_uri for track_uri in uris if track_uri]
        uris = list(dict.fromkeys(uris))  # Remove duplicates while preserving order

        if not uris:
            print("No valid track IDs found")
            return {}

        print(f"Fetching data for {len(uris)} tracks: {uris}")

        # Fetch tracks
        # Clean up the URIs to only keep the last part (spotify:track:xxxx -> xxxx)
        clean_uris = [uri.split(':')[-1] for uri in uris]
        track_info = self.spotify_client.get_tracks(clean_uris)

        if not track_info or 'tracks' not in track_info:
            print("Failed to fetch track information")
            return {}

        print(f"Fetched {len(track_info['tracks'])} tracks")

        temp_cache = {}

        # Process each track and store only needed information
        for track in track_info['tracks']:

            track_uri = track['uri']

            # Combine artist names
            artists = ", ".join([artist['name'] for artist in track['artists']])

            # Get background image URL (medium size image)
            background_image = track['album']['images'][1]['url'] if track.get('album', {}).get('images') and len(
                track['album']['images']) > 1 else None

            # Get song name
            song_name = track['name']

            # Store in cache
            temp_cache[track_uri] = {
                'song_name': song_name,
                'artists': artists,
                'background_image': background_image
            }

            print(f"Cached track: {track_uri} - {song_name} by {artists} - {background_image}")

        # Update the main cache
        self.track_cache = temp_cache

        # Pull the images to the cache
        for track_uri, track_info in temp_cache.items():
            background_image = track_info.get('background_image')
            if background_image and background_image not in self.image_cache.cache:
                # Fetch and cache the image
                await self.get_album_cover_cached_async(background_image)
            else:
                print(f"Image already cached for {track_uri}: {background_image}")

        print(f"Cached {len(self.track_cache)} tracks")

    async def fetch_loop(self):
        """Background task for Spotify API calls to prevent UI blocking"""
        print("Starting background fetch loop...")
        
        # Initial fetch
        try:
            print("Performing initial fetch...")
            fetch_start = timing.start_timer("fetch_state_initial")
            result = fetch_state(self.spotify_client)
            timing.end_timer("fetch_state_initial", fetch_start)
            
            if result:
                device_id, track, is_playing, shuffle, repeat = result
                self.state.track = track
                self.state.is_playing = is_playing
                self.state.shuffle = shuffle
                self.state.repeat = repeat
                if device_id:
                    self.spotify_client.session.device_id = device_id
                self.state.latest_fetch = time.time()

                # Add to cache
                self.track_cache = {track['uri']: {
                    'song_name': track['name'],
                    'artists': ", ".join([artist['name'] for artist in track['artists']]),
                    'background_image': track['album']['images'][1]['url'] if track.get('album', {}).get('images') and len(track['album']['images']) > 1 else None
                }}

                print(f"Initial fetch cache: {self.track_cache}")

                print(f"Initial fetch successful: {track.get('name', 'No track') if track else 'No track'}")
            else:
                print("Initial fetch returned no data")
        except Exception as e:
            print(f"Initial fetch error: {e}")
        
        while not self.state.exit:
            try:
                start_time = time.time()
                
                # Only fetch if enough time has passed
                killswitch = True
                if not killswitch: 
                #if not self.state.latest_fetch or time.time() - self.state.latest_fetch > 5:
                    fetch_start = timing.start_timer("fetch_state_background")
                    result = fetch_state(self.spotify_client)
                    timing.end_timer("fetch_state_background", fetch_start)
                    
                    if result:
                        device_id, track, is_playing, shuffle, repeat = result
                        
                        # Check if track actually changed
                        track_changed = (self.state.track or {}).get('id') != (track or {}).get('id')
                        
                        self.state.track = track
                        self.state.is_playing = is_playing
                        self.state.shuffle = shuffle
                        self.state.repeat = repeat
                        if device_id:
                            self.spotify_client.session.device_id = device_id
                        self.state.latest_fetch = time.time()
                        
                        print(f"moo: {track}")
                        
                        if track_changed and track:
                            print(f"NEW TRACK: {track.get('name', 'Unknown')}")
                        elif track:
                            print(f"Track update: {track.get('name', 'Unknown')}")
                
            except Exception as e:
                print(f"Background fetch error: {e}")
            
            await asyncio.sleep(5)  # Check every 5 seconds

    @timed_async("touch_handler_loop")
    async def touch_handler_loop(self):
        """Handles touch input events and button presses."""
        while not self.state.exit:
            touch_start = timing.start_timer("touch_poll")
            self.touch.poll()
            timing.end_timer("touch_poll", touch_start)

            button_start = timing.start_timer("button_processing")
            for button in self.buttons:
                button.update(self.state, button)
                if button.is_pressed(self.state):
                    print(f"{button.name} pressed")
                    try:
                        button.on_press(self)
                    except Exception as e:
                        print(f"Failed to execute on_press: {e}")
                    break
            timing.end_timer("button_processing", button_start)
            
            # Wait here until the user stops touching the screen
            while self.touch.state:
                self.touch.poll()

            await asyncio.sleep_ms(5)

    @timed("show_image")
    def show_image(self, img, minimized=False):
        """Displays an album cover image on the screen."""
        if not img:
            return
            
        try:
            decode_start = timing.start_timer("image_decode")
            self.j.open_RAM(memoryview(img))

            img_width, img_height = self.j.get_width(), self.j.get_height()
            img_x, img_y = (self.width - img_width) // 2, (self.height - img_height) // 2

            # Clear layer 0 and draw the image
            self.display.set_layer(0)  # Make sure we're on the right layer
            self.clear(0)
            self.j.decode(img_x, img_y, jpegdec.JPEG_SCALE_FULL, dither=True)
            self.presto.update()  # Ensure the image is actually displayed
            timing.end_timer("image_decode", decode_start)
            print(f"Image displayed at ({img_x}, {img_y}) size {img_width}x{img_height}")

        except OSError as e:
            print(f"Failed to load image: {e}")
            timing.end_timer("image_decode", decode_start)
        
    @timed("write_track")
    def write_track(self):
        """Writes the track name and artists on the screen."""
        if self.state.show_controls and self.state.track:
            text_start = timing.start_timer("text_processing")
            
            self.display.set_thickness(3)

            # Use track cache
            if self.state.track.get("uri") not in self.track_cache:
                return

            track_info = self.track_cache[self.state.track.get("uri")]
            track_name = track_info.get("song_name")
            artists = track_info.get("artists")

            # strip non-ascii characters
            track_name = ''.join(i if ord(i) < 128 else ' ' for i in track_name)
            if len(track_name) > 20:
                track_name = track_name[:20] + " ..."
            
            timing.end_timer("text_processing", text_start)
            
            render_start = timing.start_timer("text_render")
            # shadow effect
            self.display.set_pen(self.colors.BLACK)
            self.display.text(track_name, 20, self.height - 137, scale=1.1)
            
            self.display.set_pen(self.colors.WHITE)
            self.display.text(track_name, 18, self.height - 140, scale=1.1)

            # strip non-ascii characters - TODO move elsewhere
            artists = ''.join(i if ord(i) < 128 else ' ' for i in artists)
            if len(artists) > 35:
                artists = artists[:35] + " ..."
            self.display.set_thickness(2)

            # shadow effect
            self.display.set_pen(self.colors.BLACK)
            self.display.text(artists, 20, self.height - 108, scale=0.7)

            self.display.set_pen(self.colors.WHITE)
            self.display.text(artists, 18, self.height - 111, scale=0.7)
            timing.end_timer("text_render", render_start)

    @timed_async("display_loop")
    async def display_loop(self):
        """Optimized display loop focused on UI updates only"""
        prev_state = None
        initial_wait = True

        while not self.state.exit:
            loop_start = timing.start_timer("display_loop_iteration")

            # Force an initial update to show the interface
            if initial_wait:
                print("Initial display update...")
                self.clear(1)
                for button in self.buttons:
                    button.update(self.state, button)
                    button.draw(self.state)
                self.presto.update()
                initial_wait = False
                prev_state = self.state.copy()

            # Display update only when needed - NO IMAGE HANDLING
            if prev_state != self.state:
                display_start = timing.start_timer("display_update")
                
                # Only clear layer 1, not the whole screen
                clear_start = timing.start_timer("display_clear")
                self.clear(1)
                timing.end_timer("display_clear", clear_start)
                
                buttons_start = timing.start_timer("all_buttons_update")
                for button in self.buttons:
                    button.update(self.state, button)
                    button.draw(self.state)
                timing.end_timer("all_buttons_update", buttons_start)
                
                # Update track text immediately when track changes
                self.write_track()

                update_start = timing.start_timer("presto_update")
                self.presto.update()
                timing.end_timer("presto_update", update_start)
                
                prev_state = self.state.copy()
                timing.end_timer("display_update", display_start)

                print(f"UI updated for: {(self.state.track or {}).get('name') or self.state.track.get('metadata', {}).get('title') or 'Unknown' if self.state.track else 'No track'}")
                
            # Much less frequent garbage collection
            self._gc_counter += 1
            if self._gc_counter >= 50:  # Every ~5 seconds instead of every loop
                gc_start = timing.start_timer("garbage_collect")
                gc.collect()
                timing.end_timer("garbage_collect", gc_start)
                self._gc_counter = 0
            
            timing.end_timer("display_loop_iteration", loop_start)
            await asyncio.sleep_ms(25)  # Even faster UI updates

    async def timing_report_loop(self):
        """Periodically print timing reports"""
        while not self.state.exit:
            await asyncio.sleep(10)
            timing.report()

            # Also print Spotify client timing
            if hasattr(self.spotify_client.session, '__class__'):
                print("\n=== SPOTIFY HTTP CLIENT INTERNAL TIMING ===")
                # Import and access the spotify timing collector
                from applications.spotify.spotify_http_client import spotify_timing
                spotify_timing.report()
            
            print("Memory Info:")
            print(f"  Free: {gc.mem_free()} bytes")
            print(f"  Allocated: {gc.mem_alloc()} bytes") 
            print(f"  Total: {gc.mem_free() + gc.mem_alloc()} bytes")
            print(f"  Usage: {gc.mem_alloc() / (gc.mem_free() + gc.mem_alloc()) * 100:.1f}%")

@timed("fetch_state")
def fetch_state(spotify_client):
    """Fetches the current playback state from Spotify."""

    current_track = None
    is_playing = False
    shuffle = False
    repeat = False
    device_id = None
    
    api_start = timing.start_timer("spotify_api_current")
    try:
        resp = spotify_client.current_playing()
        timing.end_timer("spotify_api_current", api_start)
        
        if resp and resp.get("item"):
            current_track = resp["item"]
            is_playing = resp.get("is_playing")
            shuffle = resp.get("shuffle_state")
            repeat = resp.get("repeat_state", "off") != "off" 
            device_id = resp["device"]["id"]
            print("Got current playing track: " + current_track.get("name"))
    except Exception as e:
        timing.end_timer("spotify_api_current", api_start)
        print("Failed to get current playing track:", e)

    if not current_track:
        recent_start = timing.start_timer("spotify_api_recent")
        try:
            resp = spotify_client.recently_played()
            timing.end_timer("spotify_api_recent", recent_start)
            
            if resp and resp.get("items"):
                current_track = resp["items"][0]["track"]
                print("Got recently playing track: " + current_track.get("name"))
        except Exception as e:
            timing.end_timer("spotify_api_recent", recent_start)
            print("Failed to get recently played track:", e)

    if not current_track:
        return None

    return device_id, current_track, is_playing, shuffle, repeat

@timed("get_album_cover")
def get_album_cover(img_url):
    """Optimized album cover fetching - smaller image, faster timeout"""
    network_start = timing.start_timer("album_network_request")
    img = None
    # HTTP by default, no need for HTTPS for album pics, and it's a lot faster.
    resize_url = f"http://images.weserv.nl/?url={img_url}&w=480&h=480&fit=cover&q=80&output=jpg"
    try:
        # Reduced timeout from default to 5 seconds
        response = requests.get(resize_url, timeout=5)
        timing.end_timer("album_network_request", network_start)
        
        if response.status_code == 200:
            img = response.content
        else:
            print("Failed to fetch image:", response.status_code)
    except Exception as e:
        timing.end_timer("album_network_request", network_start)
        print("Fetch image error:", e)
        
    return img

def launch():
    """Launches the Spotify app and starts the event loop."""
    app = None
    try:
        app = Spotify()
        app.run()
    except KeyboardInterrupt:
        print("App interrupted by user")
    except Exception as e:
        print(f"Launch error: {e}")
    finally:
        if app:
            app.clear()
            del app
        gc.collect()
        print("Spotify app closed")