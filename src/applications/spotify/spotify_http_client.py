import sys
import time
import socket
import ssl
import json
import urequests as requests

class TimingCollector:
    """Optimized timing collector for Pico"""
    def __init__(self):
        self.times = {}
        self.counts = {}
        
    def start_timer(self, name):
        """Start timing using time.ticks_ms() for better precision on Pico"""
        return time.ticks_ms()
    
    def end_timer(self, name, start_time):
        """End timing and record the result"""
        duration_ms = time.ticks_diff(time.ticks_ms(), start_time)
        duration = duration_ms / 1000.0  # Convert to seconds
        
        if name not in self.times:
            self.times[name] = []
            self.counts[name] = 0
        self.times[name].append(duration)
        self.counts[name] += 1
        
        # Keep only last 3 measurements to save memory on Pico
        if len(self.times[name]) > 3:
            self.times[name] = self.times[name][-3:]
    
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
        """Print timing report"""
        print("\n=== SPOTIFY CLIENT TIMING REPORT ===")
        print(f"{'Operation':<30} | {'Avg':>8} | {'Min':>8} | {'Max':>8} | {'Count':>5}")
        print("-" * 65)
        
        for name in sorted(self.times.keys()):
            avg_time = self.get_average(name)
            min_time, max_time = self.get_min_max(name)
            count = self.counts[name]
            
            if count > 0 and avg_time > 0:
                print(f"{name:<30} | {avg_time*1000:7.1f}ms | {min_time*1000:7.1f}ms | {max_time*1000:7.1f}ms | {count:5d}")
        
        print("=" * 65)

# Global timing collector
spotify_timing = TimingCollector()

def timed_spotify(operation_name):
    """Decorator to time Spotify operations"""
    def decorator(func):
        def wrapper(*args, **kwargs):
            start_time = spotify_timing.start_timer(operation_name)
            try:
                result = func(*args, **kwargs)
                spotify_timing.end_timer(operation_name, start_time)
                return result
            except Exception as e:
                spotify_timing.end_timer(operation_name, start_time)
                raise e
        return wrapper
    return decorator

# Ultra-aggressive SSL connection reuse for Pico
class SSLOptimizedConnectionPool:
    """Ultra-aggressive SSL connection reuse for Pico"""
    
    def __init__(self):
        self.connections = {}
        self.last_used = {}
        self.max_idle = 300  # Keep connections alive longer
        # Keep connections even more aggressively
        self.max_connections = 2
    
    def get_connection(self, host, port=443):
        """Get connection with maximum SSL reuse"""
        key = f"{host}:{port}"
        now = time.time()
        
        # Very aggressive reuse - check if connection exists first
        if key in self.connections:
            # For Pico, just assume the connection is good
            # Testing connections can be problematic with limited SSL implementations
            conn = self.connections[key]
            self.last_used[key] = now
            print("Reusing existing SSL connection")
            return conn
        
        # Only clean up if we really need to
        if len(self.connections) >= self.max_connections:
            oldest_key = min(self.last_used.keys(), key=lambda k: self.last_used[k])
            self._close_connection(oldest_key)
        
        # Create new connection - measure SSL handshake time
        print(f"Creating new SSL connection to {host} (expensive operation)")
        try:
            # Create socket with minimal timeout
            socket_start = time.ticks_ms()
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(8)  # Slightly shorter timeout
            
            # Connect
            addr = socket.getaddrinfo(host, port)[0][-1]
            sock.connect(addr)
            socket_time = time.ticks_diff(time.ticks_ms(), socket_start)
            
            # SSL wrap - this is the expensive part
            ssl_start = time.ticks_ms()
            ssl_sock = ssl.wrap_socket(sock)
            ssl_time = time.ticks_diff(time.ticks_ms(), ssl_start)

            print(f"Socket connect: {socket_time}ms, SSL handshake: {ssl_time}ms")
            
            # Cache aggressively
            self.connections[key] = ssl_sock
            self.last_used[key] = now
            return ssl_sock
            
        except Exception as e:
            print(f"SSL connection failed: {e}")
            raise
    
    def _close_connection(self, key):
        """Close connection only when absolutely necessary"""
        print(f"Closing SSL connection to {key}")
        if key in self.connections:
            try:
                self.connections[key].close()
            except:
                pass
            del self.connections[key]
        if key in self.last_used:
            del self.last_used[key]

# Global optimized connection pool
connection_pool = SSLOptimizedConnectionPool()

# SSL-optimized HTTP session that FORCES use of manual implementation
class SSLOptimizedSession:
    """Session optimized to minimize SSL overhead - forces manual SSL implementation"""
    
    def __init__(self):
        self.default_headers = {}
        # Track last request time to keep connection warm
        self.last_request_time = 0
        # Force use of manual implementation for better SSL control
        self.force_manual_mode = True
    
    def set_default_headers(self, headers):
        self.default_headers = headers.copy()
    
    def request(self, method, url, headers=None, json=None, timeout=8, form_data=None):
        """Always use manual SSL implementation for maximum optimization"""
        
        # This ensures we get our SSL connection reuse benefits
        print(f"FORCING manual SSL implementation for request: {url}")
        return self._manual_ssl_request(method, url, headers, json, timeout, form_data)
    
    def _manual_ssl_request(self, method, url, headers=None, json=None, timeout=8, form_data=None):
        """Manual SSL request with aggressive connection reuse"""
        
        # Parse URL
        if url.startswith('https://'):
            url = url[8:]
            
        if '/' in url:
            host, path = url.split('/', 1)
            path = '/' + path
        else:
            host = url
            path = '/'
        
        # Get connection (this is where SSL reuse happens)
        connection_start = spotify_timing.start_timer("ssl_connection_get")
        conn = connection_pool.get_connection(host, 443)
        spotify_timing.end_timer("ssl_connection_get", connection_start)
        
        # Merge headers
        req_headers = self.default_headers.copy()
        if headers:
            req_headers.update(headers)
        
        # Build minimal request
        request_start = spotify_timing.start_timer("ssl_http_request")
        try:
            body = ""
            if json:
                body = json.dumps(json)
                req_headers['Content-Type'] = 'application/json'
                req_headers['Content-Length'] = str(len(body))
            elif form_data:
                # For form data (like token refresh)
                body = form_data
                req_headers['Content-Length'] = str(len(body))
                # Content-Type should already be set to application/x-www-form-urlencoded
            else:
                # For requests without body (like next/previous), still need Content-Length
                req_headers['Content-Length'] = '0'
            
            # Minimal HTTP request
            http_request = f"{method} {path} HTTP/1.1\r\n"
            http_request += f"Host: {host}\r\n"
            for name, value in req_headers.items():
                http_request += f"{name}: {value}\r\n"
            http_request += "Connection: keep-alive\r\n"
            http_request += "\r\n"
            
            if body:
                http_request += body
            
            # Send and receive (reusing SSL connection)
            conn.send(http_request.encode())
            
            # Minimal response parsing
            response_data = b""
            while b"\r\n\r\n" not in response_data:
                chunk = conn.recv(1024)
                if not chunk:
                    break
                response_data += chunk
            
            # Quick response parsing
            headers_end = response_data.find(b"\r\n\r\n")
            headers_text = response_data[:headers_end].decode()
            body_start = headers_end + 4
            
            status_line = headers_text.split('\r\n')[0]
            status_code = int(status_line.split()[1])
            
            # Read body if needed
            content_length = 0
            for line in headers_text.split('\r\n')[1:]:
                if line.lower().startswith('content-length:'):
                    content_length = int(line.split(':')[1].strip())
                    break
            
            body_data = response_data[body_start:]
            while len(body_data) < content_length:
                chunk = conn.recv(1024)
                if not chunk:
                    break
                body_data += chunk
            
            # Track last request time to keep connection warm
            self.last_request_time = time.time()
            spotify_timing.end_timer("ssl_http_request", request_start)
            
            # Simple response object
            class Response:
                def __init__(self, status_code, content, headers_text):
                    self.status_code = status_code
                    self.content = content
                    self.text = content.decode('utf-8') if content else ""
                    self.headers = self._parse_headers(headers_text)
                
                def _parse_headers(self, headers_text):
                    headers = {}
                    for line in headers_text.split('\r\n')[1:]:
                        if ':' in line:
                            name, value = line.split(':', 1)
                            headers[name.strip().lower()] = value.strip()
                    return headers
                
                def json(self):
                    return json.loads(self.content) if self.content else None
                
                def close(self):
                    pass
            
            return Response(status_code, body_data, headers_text)
            
        except Exception as e:
            spotify_timing.end_timer("ssl_http_request", request_start)
            raise
    
    def get(self, url, **kwargs):
        return self.request('GET', url, **kwargs)
    
    def post(self, url, **kwargs):
        return self.request('POST', url, **kwargs)
    
    def put(self, url, **kwargs):
        return self.request('PUT', url, **kwargs)

# Global SSL-optimized session
spotify_session = SSLOptimizedSession()

class SpotifyWebApiClient:
    def __init__(self, session):
        self.session = session

    @timed_spotify("client_play")
    def play(self, context_uri=None, uris=None, offset=None, position_ms=None):
        request_body = {}
        if context_uri is not None:
            request_body['context_uri'] = context_uri
        if uris is not None:
            request_body['uris'] = list(uris)
        if offset is not None:
            request_body['offset'] = offset
        if position_ms is not None:
            request_body['position_ms'] = position_ms

        self.session.put(
            url='https://api.spotify.com/v1/me/player/play',
            json=request_body,
        )

    @timed_spotify("client_pause")
    def pause(self):
        self.session.put(
            url='https://api.spotify.com/v1/me/player/pause',
        )
    
    @timed_spotify("client_shuffle")
    def toggle_shuffle(self, state):
        value = "true" if state else "false"
        self.session.put(
            url=f'https://api.spotify.com/v1/me/player/shuffle?state={value}',
        )
    
    @timed_spotify("client_repeat")
    def toggle_repeat(self, state):
        value = "track" if state else 'off'
        self.session.put(
            url=f'https://api.spotify.com/v1/me/player/repeat?state={value}',
        )
    
    @timed_spotify("client_next")
    def next(self):
        self.session.post(
            url='https://api.spotify.com/v1/me/player/next',
        )

    @timed_spotify("client_previous")
    def previous(self):
        self.session.post(
            url='https://api.spotify.com/v1/me/player/previous',
        )

    @timed_spotify("client_current_playing")
    def current_playing(self):
        return self.session.get(
            url='https://api.spotify.com/v1/me/player',
        )
    
    @timed_spotify("client_recently_played")
    def recently_played(self):
        return self.session.get(
            url='https://api.spotify.com/v1/me/player/recently-played?limit=1',
        )

    @timed_spotify("client_get_tracks")
    def get_tracks(self, track_ids):
        ids = ','.join(track_ids)
        return self.session.get(
            url=f'https://api.spotify.com/v1/tracks?ids={ids}',
        )

class Device:
    def __init__(
        self,
        id,
        is_active,
        is_private_session,
        is_restricted,
        name,
        type,
        volume_percent,
        **kwargs
    ):
        self.id = id
        self.is_active = is_active
        self.is_private_session = is_private_session
        self.is_restricted = is_restricted
        self.name = name
        self.type = type
        self.volume_percent = volume_percent

    def __repr__(self):
        return 'Device(name={}, type={}, id={})'.format(self.name, self.type, self.id)

class Session:
    def __init__(self, credentials):
        self.credentials = credentials
        self.device_id = credentials['device_id']
        if 'access_token' not in credentials:
            self._refresh_access_token()
        
        # Set up persistent session with default headers
        self._setup_persistent_session()
    
    def _setup_persistent_session(self):
        """Configure the persistent session with default headers"""
        headers = {'Authorization': 'Bearer {access_token}'.format(**self.credentials)}
        spotify_session.set_default_headers(headers)
        print("Spotify session configured with default headers")

    @timed_spotify("session_get")
    def get(self, url, **kwargs):
        """Use persistent session for GET requests"""
        network_start = spotify_timing.start_timer("session_get_network")
        try:
            response = spotify_session.get(url, **kwargs)
            spotify_timing.end_timer("session_get_network", network_start)
            return self._process_response(response)
        except Exception as e:
            spotify_timing.end_timer("session_get_network", network_start)
            # Handle token expiration
            if hasattr(e, 'response') and e.response.status_code == 401:
                return self._handle_token_refresh_and_retry(lambda: spotify_session.get(url, **kwargs))
            raise

    @timed_spotify("session_put")
    def put(self, url, json=None, **kwargs):
        """Use persistent session for PUT requests"""
        if json is None:
            json = {}
        
        # Add device ID to URL
        device_start = spotify_timing.start_timer("session_add_device_id")
        full_url = self._add_device_id(url)
        spotify_timing.end_timer("session_add_device_id", device_start)
        
        network_start = spotify_timing.start_timer("session_put_network")
        try:
            response = spotify_session.put(full_url, json=json, **kwargs)
            spotify_timing.end_timer("session_put_network", network_start)
            return self._process_response(response)
        except Exception as e:
            spotify_timing.end_timer("session_put_network", network_start)
            # Handle token expiration
            if hasattr(e, 'response') and e.response.status_code == 401:
                return self._handle_token_refresh_and_retry(lambda: spotify_session.put(full_url, json=json, **kwargs))
            raise
    
    @timed_spotify("session_post")
    def post(self, url, json=None, **kwargs):
        """Use persistent session for POST requests"""
        if json is None:
            json = {}
        
        # Add device ID to URL
        device_start = spotify_timing.start_timer("session_add_device_id")
        full_url = self._add_device_id(url)
        spotify_timing.end_timer("session_add_device_id", device_start)
        
        network_start = spotify_timing.start_timer("session_post_network")
        try:
            response = spotify_session.post(full_url, json=json, **kwargs)
            spotify_timing.end_timer("session_post_network", network_start)
            return self._process_response(response)
        except Exception as e:
            spotify_timing.end_timer("session_post_network", network_start)
            # Handle token expiration  
            if hasattr(e, 'response') and e.response.status_code == 401:
                return self._handle_token_refresh_and_retry(lambda: spotify_session.post(full_url, json=json, **kwargs))
            raise

    def _process_response(self, response):
        """Process HTTP response with timing"""
        status_start = spotify_timing.start_timer("session_check_status")
        self._check_status_code(response)
        spotify_timing.end_timer("session_check_status", status_start)
        
        json_start = spotify_timing.start_timer("session_parse_json")
        content_type = response.headers.get("content-type")
        if response.content and content_type and content_type.startswith("application/json"):
            resp_json = response.json()
            response.close()
            spotify_timing.end_timer("session_parse_json", json_start)
            return resp_json
        spotify_timing.end_timer("session_parse_json", json_start)
        response.close()
        return None

    def _handle_token_refresh_and_retry(self, request_func):
        """Handle token refresh and retry request"""
        token_start = spotify_timing.start_timer("session_refresh_token")
        self._refresh_access_token()
        # Update the persistent session with new token
        self._setup_persistent_session()
        spotify_timing.end_timer("session_refresh_token", token_start)
        
        # Retry the request
        response = request_func()
        return self._process_response(response)

    @staticmethod
    def _check_status_code(response):
        if response.status_code >= 400:
            error = Session._error_from_response(response)
            response.close()
            raise SpotifyWebApiError(**error)

    @staticmethod
    def _error_from_response(response):
        try:
            error = response.json()['error']
            message = error['message']
            reason = error.get('reason')
        except (ValueError, KeyError):
            message = response.text
            reason = None
        return {'message': message, 'status': response.status_code, 'reason': reason}

    def _add_device_id(self, url):
        join = '&' if '?' in url else '?'
        return '{path}{join}device_id={device_id}'.format(path=url, join=join, device_id=self.device_id) if self.device_id else url

    @timed_spotify("session_refresh_access_token")
    def _refresh_access_token(self):
        token_endpoint = "https://accounts.spotify.com/api/token"
        params = dict(
            grant_type="refresh_token",
            refresh_token=self.credentials['refresh_token'],
            client_id=self.credentials['client_id'],
            client_secret=self.credentials['client_secret'],
        )
        retries = 3
        while retries:
            network_start = spotify_timing.start_timer("token_refresh_network")
            try:
                # Create form data and proper headers
                form_data = urlencode(params)
                headers = {'Content-Type': 'application/x-www-form-urlencoded'}

                response = spotify_session.request(
                    'POST', token_endpoint,
                    headers=headers,
                    json=None,  # Don't use json
                    timeout=8,
                    form_data=form_data  # Pass form data
                )
                
                spotify_timing.end_timer("token_refresh_network", network_start)
                self._check_status_code(response)
                break  # Success - exit retry loop
            except Exception as E:
                spotify_timing.end_timer("token_refresh_network", network_start)
                print(f"Token refresh error: {E}")
                retries -= 1
                if retries:
                    print("Failed to refresh access token, retrying")
                else:
                    print("Failed to refresh access token after 3 tries, giving up")
                    return  # Early exit on failure

        tokens = response.json()
        response.close()
        self.credentials['access_token'] = tokens['access_token']
        if 'refresh_token' in tokens:
            self.credentials['refresh_token'] = tokens['refresh_token']

class SpotifyWebApiError(Exception):
    def __init__(self, message, status=None, reason=None):
        super().__init__(message)
        self.status = status
        self.reason = reason

def quote(s):
    always_safe = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ' 'abcdefghijklmnopqrstuvwxyz' '0123456789' '_.-'
    res = []
    for c in s:
        if c in always_safe:
            res.append(c)
            continue
        res.append('%%%02x' % ord(c))  # Fixed: use %02x for proper hex formatting
    return ''.join(res)

def quote_plus(s):
    s = quote(s)
    if ' ' in s:
        s = s.replace(' ', '+')
    return s

def unquote(s):
    res = s.split('%')
    for i in range(1, len(res)):
        item = res[i]
        try:
            res[i] = chr(int(item[:2], 16)) + item[2:]
        except ValueError:
            res[i] = '%' + item
    return "".join(res)

def urlencode(query):
    if isinstance(query, dict):
        query = query.items()
    li = []
    for k, v in query:
        if not isinstance(v, list):
            v = [v]
        for value in v:
            k = quote_plus(str(k))
            v = quote_plus(str(value))
            li.append(k + '=' + v)
    return '&'.join(li)