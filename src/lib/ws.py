# Modified from https://github.com/Vovaman/micropython_async_websocket_client

import socket
import asyncio as a
import binascii as b
import random as r
from collections import namedtuple
import re
import struct
import ssl

# Opcodes
OP_CONT = 0x0
OP_TEXT = 0x1
OP_BYTES = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xa

# Close codes
CLOSE_OK = 1000
CLOSE_GOING_AWAY = 1001
CLOSE_PROTOCOL_ERROR = 1002
CLOSE_DATA_NOT_SUPPORTED = 1003
CLOSE_BAD_DATA = 1007
CLOSE_POLICY_VIOLATION = 1008
CLOSE_TOO_BIG = 1009
CLOSE_MISSING_EXTN = 1010
CLOSE_BAD_CONDITION = 1011

URL_RE = re.compile(r'(wss|ws)://([A-Za-z0-9-\.]+)(?:\:([0-9]+))?(/.+)?')
URI = namedtuple('URI', ('protocol', 'hostname', 'port', 'path'))

class AsyncWebsocketClient:
    def __init__(self, ms_delay_for_read: int = 5):
        self._open = False
        self.delay_read = ms_delay_for_read
        self._lock_for_open = a.Lock()
        self.sock = None

    async def open(self, new_val: bool = None):
        await self._lock_for_open.acquire()
        if new_val is not None:
            if not new_val and self.sock:
                self.sock.close()
                self.sock = None
            self._open = new_val
        to_return = self._open
        self._lock_for_open.release()
        return to_return

    async def close(self):
        return await self.open(False)

    def urlparse(self, uri):
        """Parse ws or wss:// URLs"""
        match = URL_RE.match(uri)
        if match:
            protocol, host, port, path = match.group(1), match.group(2), match.group(3), match.group(4)

            if protocol not in ['ws', 'wss']:
                raise ValueError('Scheme {} is invalid'.format(protocol))

            if port is None:
                port = (80, 443)[protocol == 'wss']

            return URI(protocol, host, int(port), path)

    async def a_readline(self):
        line = None
        while line is None:
            line = self.sock.readline()
            await a.sleep(self.delay_read / 1000)
        return line

    async def a_read(self, size: int = None):
        if size == 0:
            return b''
        chunks = []

        while True:
            b = self.sock.read(size)
            await a.sleep(self.delay_read / 1000)

            if b is None: continue
            if len(b) == 0: break

            chunks.append(b)
            size -= len(b)

            if size is None or size == 0: break

        return b''.join(chunks)

    async def handshake(self, uri, headers=[], keyfile=None, certfile=None, cafile=None, cert_reqs=0):
        if self.sock:
            await self.close()

        self.sock = socket.socket()
        self.uri = self.urlparse(uri)
        
        # Enhanced DNS resolution and connection logic
        try:
            ai = socket.getaddrinfo(self.uri.hostname, self.uri.port)
            addr = ai[0][4]
            print(f"Connecting to {self.uri.hostname}:{self.uri.port} ({addr[0]})")
        except Exception as e:
            print(f"DNS resolution failed for {self.uri.hostname}: {e}")
            raise
        
        try:
            self.sock.connect(addr)
            self.sock.setblocking(False)
        except Exception as e:
            print(f"Socket connection failed: {e}")
            raise

        if self.uri.protocol == 'wss':
            cadata = None
            if not cafile is None:
                with open(cafile, 'rb') as f:
                    cadata = f.read()
            try:
                self.sock = ssl.wrap_socket(
                    self.sock, server_side=False,
                    key=keyfile, cert=certfile,
                    cert_reqs=cert_reqs, # 0 - NONE, 1 - OPTIONAL, 2 - REQUIED
                    cadata=cadata,
                    server_hostname=self.uri.hostname
                )
                print(f"SSL handshake completed for {self.uri.hostname}")
            except Exception as e:
                print(f"SSL handshake failed: {e}")
                raise

        def send_header(header, *args):
            self.sock.write(header % args + b'\r\n')

        # Sec-WebSocket-Key is 16 bytes of random base64 encoded
        key = b.b2a_base64(bytes(r.getrandbits(8) for _ in range(16)))[:-1]

        # Build WebSocket handshake with proper path and authorization
        path_with_query = self.uri.path or '/'
        if '?' in uri:
            # Extract query from original URI
            query_part = uri.split('?', 1)[1]
            path_with_query = f"{path_with_query}?{query_part}"
        
        #print(f"WebSocket handshake path: {path_with_query}")
        
        send_header(b'GET %s HTTP/1.1', path_with_query.encode())
        send_header(b'Host: %s:%s', self.uri.hostname.encode(), str(self.uri.port).encode())
        send_header(b'Connection: Upgrade')
        send_header(b'Upgrade: websocket')
        send_header(b'Sec-WebSocket-Key: %s', key)
        send_header(b'Sec-WebSocket-Version: 13')
        send_header(b'Origin: https://%s', self.uri.hostname.encode())
        send_header(b'User-Agent: Mozilla/5.0 (compatible; MicroPython)')

        for header_name, header_value in headers:
            # Ensure header values are bytes
            if isinstance(header_name, str):
                header_name = header_name.encode()
            if isinstance(header_value, str):
                header_value = header_value.encode()
            send_header(b'%s: %s', header_name, header_value)

        send_header(b'')

        # Read handshake response
        #print("Reading WebSocket handshake response...")
        line = await self.a_readline()
        header = (line)[:-2]
        #print(f"WebSocket response: {header}")
        
        if not header.startswith(b'HTTP/1.1 101 '):
            # Enhanced error reporting for WebSocket handshake failures
            if header.startswith(b'HTTP/1.1 401'):
                print("WebSocket authentication failed - check access token")
            elif header.startswith(b'HTTP/1.1 403'):
                print("WebSocket forbidden - check permissions")
            else:
                print(f"WebSocket handshake failed with: {header}")
            raise Exception(header)

        # Read remaining headers
        while header:
            line = await self.a_readline()
            header = (line)[:-2]
            if header:
                print(f"Header: {header}")

        print("WebSocket handshake successful!")
        return await self.open(True)

    async def read_frame(self, max_size=None):
        # Frame header
        byte1, byte2 = struct.unpack('!BB', await self.a_read(2))

        # Byte 1: FIN(1) _(1) _(1) _(1) OPCODE(4)
        fin = bool(byte1 & 0x80)
        opcode = byte1 & 0x0f

        # Byte 2: MASK(1) LENGTH(7)
        mask = bool(byte2 & (1 << 7))
        length = byte2 & 0x7f

        if length == 126:  # Magic number, length header is 2 bytes
            length, = struct.unpack('!H', await self.a_read(2))
        elif length == 127:  # Magic number, length header is 8 bytes
            length, = struct.unpack('!Q', await self.a_read(8))

        if mask:  # Mask is 4 bytes
            mask_bits = await self.a_read(4)

        try:
            data = await self.a_read(length)
        except MemoryError:
            # We can't receive this many bytes, close the socket
            await self.close()
            return True, OP_CLOSE, None

        if mask:
            data = bytes(b ^ mask_bits[i % 4] for i, b in enumerate(data))

        return fin, opcode, data

    def write_frame(self, opcode, data=b''):
        fin = True
        mask = True  # messages sent by client are masked

        length = len(data)

        # Frame header
        # Byte 1: FIN(1) _(1) _(1) _(1) OPCODE(4)
        byte1 = 0x80 if fin else 0
        byte1 |= opcode

        # Byte 2: MASK(1) LENGTH(7)
        byte2 = 0x80 if mask else 0

        if length < 126:  # 126 is magic value to use 2-byte length header
            byte2 |= length
            self.sock.write(struct.pack('!BB', byte1, byte2))

        elif length < (1 << 16):  # Length fits in 2-bytes
            byte2 |= 126  # Magic code
            self.sock.write(struct.pack('!BBH', byte1, byte2, length))

        elif length < (1 << 64):
            byte2 |= 127  # Magic code
            self.sock.write(struct.pack('!BBQ', byte1, byte2, length))

        else:
            raise ValueError()

        if mask:  # Mask is 4 bytes
            mask_bits = struct.pack('!I', r.getrandbits(32))
            self.sock.write(mask_bits)
            data = bytes(b ^ mask_bits[i % 4] for i, b in enumerate(data))

        self.sock.write(data)

    async def recv(self):
        while await self.open():
            try:
                fin, opcode, data = await self.read_frame()
            except Exception as ex:
                print('Exception in recv while reading frame:', ex)
                await self.open(False)
                return

            if not fin:
                raise NotImplementedError()

            if opcode == OP_TEXT:
                return data.decode('utf-8')
            elif opcode == OP_BYTES:
                return data
            elif opcode == OP_CLOSE:
                await self.open(False)
                return
            elif opcode == OP_PONG:
                # Ignore this frame, keep waiting for a data frame
                continue
            elif opcode == OP_PING:
                try:
                    # We need to send a pong frame
                    self.write_frame(OP_PONG, data)
                    # And then continue to wait for a data frame
                    continue
                except Exception as ex:
                    print('Error sending pong frame:', ex)
                    # If sending the pong frame fails, close the connection
                    await self.open(False)
                    return
            elif opcode == OP_CONT:
                # This is a continuation of a previous frame
                raise NotImplementedError(opcode)
            else:
                raise ValueError(opcode)

    async def send(self, buf):
        if not await self.open():
            return
        if isinstance(buf, str):
            opcode = OP_TEXT
            buf = buf.encode('utf-8')
        elif isinstance(buf, bytes):
            opcode = OP_BYTES
        else:
            raise TypeError()
        self.write_frame(opcode, buf)
