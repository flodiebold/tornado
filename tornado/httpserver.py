#!/usr/bin/env python
#
# Copyright 2009 Facebook
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""A non-blocking, single-threaded HTTP server.

Typical applications have little direct interaction with the `HTTPServer`
class except to start a server at the beginning of the process
(and even that is often done indirectly via `tornado.web.Application.listen`).

This module also defines the `HTTPRequest` class which is exposed via
`tornado.web.RequestHandler.request`.
"""

import errno
import logging
import os
import socket
import time
import urlparse

from tornado.escape import utf8, native_str, parse_qs_bytes
from tornado import httputil
from tornado import ioloop
from tornado import iostream
from tornado import netutil
from tornado import process
from tornado import stack_context
from tornado.util import b, bytes_type

try:
    import ssl # Python 2.6+
except ImportError:
    ssl = None


class HTTPServer(object):
    r"""A non-blocking, single-threaded HTTP server.

    A server is defined by a request callback that takes an HTTPRequest
    instance as an argument and writes a valid HTTP response with
    request.write(). request.finish() finishes the request (but does not
    necessarily close the connection in the case of HTTP/1.1 keep-alive
    requests). A simple example server that echoes back the URI you
    requested::

        import httpserver
        import ioloop

        def handle_request(request):
           message = "You requested %s\n" % request.uri
           request.write("HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (
                         len(message), message))
           request.finish()

        http_server = httpserver.HTTPServer(handle_request)
        http_server.listen(8888)
        ioloop.IOLoop.instance().start()

    HTTPServer is a very basic connection handler. Beyond parsing the
    HTTP request body and headers, the only HTTP semantics implemented
    in HTTPServer is HTTP/1.1 keep-alive connections. We do not, however,
    implement chunked encoding, so the request callback must provide a
    Content-Length header or implement chunked encoding for HTTP/1.1
    requests for the server to run correctly for HTTP/1.1 clients. If
    the request handler is unable to do this, you can provide the
    no_keep_alive argument to the HTTPServer constructor, which will
    ensure the connection is closed on every request no matter what HTTP
    version the client is using.

    If connection_timeout is set, HTTP keep-alive connections will be closed
    after that many seconds of inactivity.

    If xheaders is True, we support the X-Real-Ip and X-Scheme headers,
    which override the remote IP and HTTP scheme for all requests. These
    headers are useful when running Tornado behind a reverse proxy or
    load balancer.

    HTTPServer can serve HTTPS (SSL) traffic with Python 2.6+ and OpenSSL.
    To make this server serve SSL traffic, send the ssl_options dictionary
    argument with the arguments required for the ssl.wrap_socket() method,
    including "certfile" and "keyfile"::

       HTTPServer(applicaton, ssl_options={
           "certfile": os.path.join(data_dir, "mydomain.crt"),
           "keyfile": os.path.join(data_dir, "mydomain.key"),
       })

    HTTPServer initialization follows one of three patterns:

    1. `listen`: simple single-process::

            server = HTTPServer(app)
            server.listen(8888)
            IOLoop.instance().start()

       In many cases, `tornado.web.Application.listen` can be used to avoid
       the need to explicitly create the ``HTTPServer``.

    2. `bind`/`start`: simple multi-process::
   
            server = HTTPServer(app)
            server.bind(8888)
            server.start(0)  # Forks multiple sub-processes
            IOLoop.instance().start()

       When using this interface, an ``IOLoop`` must *not* be passed
       to the ``HTTPServer`` constructor.  `start` will always start
       the server on the default singleton ``IOLoop``.

    3. `add_sockets`: advanced multi-process::

            sockets = tornado.netutil.bind_sockets(8888)
            tornado.process.fork_processes(0)
            server = HTTPServer(app)
            server.add_sockets(sockets)
            IOLoop.instance().start()

       The `add_sockets` interface is more complicated, but it can be
       used with `tornado.process.fork_processes` to give you more
       flexibility in when the fork happens.  ``add_sockets`` can
       also be used in single-process servers if you want to create
       your listening sockets in some way other than 
       `tornado.netutil.bind_sockets`.
    """
    def __init__(self, request_callback, no_keep_alive=False, io_loop=None,
                 xheaders=False, ssl_options=None, connection_timeout=-1):
        self.request_callback = request_callback
        self.no_keep_alive = no_keep_alive
        self.connection_timeout = connection_timeout
        self.io_loop = io_loop
        self.xheaders = xheaders
        self.ssl_options = ssl_options
        self._sockets = {}  # fd -> socket object
        self._pending_sockets = []
        self._started = False

    def listen(self, port, address=""):
        """Starts accepting connections on the given port.

        This method may be called more than once to listen on multiple ports.
        ``listen`` takes effect immediately; it is not necessary to call
        `HTTPServer.start` afterwards.  It is, however, necessary to start
        the ``IOLoop``.
        """
        sockets = netutil.bind_sockets(port, address=address)
        self.add_sockets(sockets)

    def add_sockets(self, sockets):
        """Makes this server start accepting connections on the given sockets.

        The ``sockets`` parameter is a list of socket objects such as
        those returned by `tornado.netutil.bind_sockets`.
        ``add_sockets`` is typically used in combination with that
        method and `tornado.process.fork_processes` to provide greater
        control over the initialization of a multi-process server.
        """
        if self.io_loop is None:
            self.io_loop = ioloop.IOLoop.instance()
        for sock in sockets:
            self._sockets[sock.fileno()] = sock
            netutil.add_accept_handler(sock, self._handle_connection,
                                       io_loop=self.io_loop)

    def add_socket(self, socket):
        """Singular version of `add_sockets`.  Takes a single socket object."""
        self.add_sockets([socket])

    def bind(self, port, address=None, family=socket.AF_UNSPEC, backlog=128):
        """Binds this server to the given port on the given address.

        To start the server, call start(). If you want to run this server
        in a single process, you can call listen() as a shortcut to the
        sequence of bind() and start() calls.

        Address may be either an IP address or hostname.  If it's a hostname,
        the server will listen on all IP addresses associated with the
        name.  Address may be an empty string or None to listen on all
        available interfaces.  Family may be set to either socket.AF_INET
        or socket.AF_INET6 to restrict to ipv4 or ipv6 addresses, otherwise
        both will be used if available.

        The ``backlog`` argument has the same meaning as for 
        ``socket.listen()``.

        This method may be called multiple times prior to start() to listen
        on multiple ports or interfaces.
        """
        sockets = netutil.bind_sockets(port, address=address,
                                       family=family, backlog=backlog)
        if self._started:
            self.add_sockets(sockets)
        else:
            self._pending_sockets.extend(sockets)

    def start(self, num_processes=1):
        """Starts this server in the IOLoop.

        By default, we run the server in this process and do not fork any
        additional child process.

        If num_processes is None or <= 0, we detect the number of cores
        available on this machine and fork that number of child
        processes. If num_processes is given and > 1, we fork that
        specific number of sub-processes.

        Since we use processes and not threads, there is no shared memory
        between any server code.

        Note that multiple processes are not compatible with the autoreload
        module (or the debug=True option to tornado.web.Application).
        When using multiple processes, no IOLoops can be created or
        referenced until after the call to HTTPServer.start(n).
        """
        assert not self._started
        self._started = True
        if num_processes != 1:
            process.fork_processes(num_processes)
        sockets = self._pending_sockets
        self._pending_sockets = []
        self.add_sockets(sockets)

    def stop(self):
        """Stops listening for new connections.

        Requests currently in progress may still continue after the
        server is stopped.
        """
        for fd, sock in self._sockets.iteritems():
            self.io_loop.remove_handler(fd)
            sock.close()

    def _handle_connection(self, connection, address):
        if self.ssl_options is not None:
            assert ssl, "Python 2.6+ and OpenSSL required for SSL"
            try:
                connection = ssl.wrap_socket(connection,
                                             server_side=True,
                                             do_handshake_on_connect=False,
                                             **self.ssl_options)
            except ssl.SSLError, err:
                if err.args[0] == ssl.SSL_ERROR_EOF:
                    return connection.close()
                else:
                    raise
            except socket.error, err:
                if err.args[0] == errno.ECONNABORTED:
                    return connection.close()
                else:
                    raise
        try:
            if self.ssl_options is not None:
                stream = iostream.SSLIOStream(connection, io_loop=self.io_loop)
            else:
                stream = iostream.IOStream(connection, io_loop=self.io_loop)
            if connection.family not in (socket.AF_INET, socket.AF_INET6):
                # Unix (or other) socket; fake the remote address
                address = ('0.0.0.0', 0)
            HTTPConnection(stream, address, self.request_callback,
                           self.no_keep_alive, self.xheaders,
                           self.connection_timeout)
        except Exception:
            logging.error("Error in connection callback", exc_info=True)

class _BadRequestException(Exception):
    """Exception class for malformed HTTP requests."""
    pass

class HTTPConnection(object):
    """Handles a connection to an HTTP client, executing HTTP requests.

    We parse HTTP headers and bodies, and execute the request callback
    until the HTTP conection is closed.
    """
    def __init__(self, stream, address, request_callback, no_keep_alive=False,
                 xheaders=False, connection_timeout=-1):
        self.stream = stream
        self.address = address
        self.request_callback = request_callback
        self.no_keep_alive = no_keep_alive
        self.connection_timeout = connection_timeout
        self.xheaders = xheaders
        self._request = None
        self._request_finished = False
        # Save stack context here, outside of any request.  This keeps
        # contexts from one request from leaking into the next.
        self._header_callback = stack_context.wrap(self._on_headers)
        self.stream.read_until(b("\r\n\r\n"), self._header_callback)
        self._timeout_handle = None
        self.reset_connection_timeout()

    def reset_connection_timeout(self):
        if self.connection_timeout == -1:
            return
        self.remove_connection_timeout()
        self._timeout_handle = self.stream.io_loop.add_timeout(
            time.time() + self.connection_timeout, self._handle_timeout)

    def remove_connection_timeout(self):
        if self._timeout_handle:
            self.stream.io_loop.remove_timeout(self._timeout_handle)

    def _handle_timeout(self):
        if self.stream.closed():
            return
        
        if self.stream.writing():
            self.reset_connection_timeout()
        else:
            self.stream.close()

    def write(self, chunk):
        """Writes a chunk of output to the stream."""
        assert self._request, "Request closed"
        if not self.stream.closed():
            self.stream.write(chunk, self._on_write_complete)

    def finish(self):
        """Finishes the request."""
        assert self._request, "Request closed"
        self._request_finished = True
        if not self.stream.writing():
            self._finish_request()

    def _on_write_complete(self):
        if self._request_finished:
            self._finish_request()

    def _finish_request(self):
        if self.no_keep_alive:
            disconnect = True
        else:
            connection_header = self._request.headers.get("Connection")
            if self._request.supports_http_1_1():
                disconnect = connection_header == "close"
            elif ("Content-Length" in self._request.headers
                    or self._request.method in ("HEAD", "GET")):
                disconnect = connection_header != "Keep-Alive"
            else:
                disconnect = True
        self._request = None
        self._request_finished = False
        if disconnect:
            self.stream.close()
            return
        else:
            self.reset_connection_timeout()
        self.stream.read_until(b("\r\n\r\n"), self._header_callback)

    def _on_headers(self, data):
        try:
            self.reset_connection_timeout()
            data = native_str(data.decode('latin1'))
            eol = data.find("\r\n")
            start_line = data[:eol]
            try:
                method, uri, version = start_line.split(" ")
            except ValueError:
                raise _BadRequestException("Malformed HTTP request line")
            if not version.startswith("HTTP/"):
                raise _BadRequestException("Malformed HTTP version in HTTP Request-Line")
            headers = httputil.HTTPHeaders.parse(data[eol:])
            self._request = HTTPRequest(
                connection=self, method=method, uri=uri, version=version,
                headers=headers, remote_ip=self.address[0])

            content_length = headers.get("Content-Length")
            if content_length:
                content_length = int(content_length)
                if content_length > self.stream.max_buffer_size:
                    raise _BadRequestException("Content-Length too long")
                if headers.get("Expect") == "100-continue":
                    self.stream.write("HTTP/1.1 100 (Continue)\r\n\r\n")
                self.stream.read_bytes(content_length, self._on_request_body)
                return

            self.request_callback(self._request)
        except _BadRequestException, e:
            logging.info("Malformed HTTP request from %s: %s",
                         self.address[0], e)
            self.stream.close()
            return

    def _on_request_body(self, data):
        self.reset_connection_timeout()
        self._request.body = data
        content_type = self._request.headers.get("Content-Type", "")
        if self._request.method in ("POST", "PUT"):
            if content_type.startswith("application/x-www-form-urlencoded"):
                arguments = parse_qs_bytes(native_str(self._request.body))
                for name, values in arguments.iteritems():
                    values = [v for v in values if v]
                    if values:
                        self._request.arguments.setdefault(name, []).extend(
                            values)
            elif content_type.startswith("multipart/form-data"):
                fields = content_type.split(";")
                for field in fields:
                    k, sep, v = field.strip().partition("=")
                    if k == "boundary" and v:
                        httputil.parse_multipart_form_data(
                            utf8(v), data,
                            self._request.arguments,
                            self._request.files)
                        break
                else:
                    logging.warning("Invalid multipart/form-data")
        self.request_callback(self._request)


class HTTPRequest(object):
    """A single HTTP request.

    .. attribute:: method

       HTTP request method, e.g. "GET" or "POST"

    .. attribute:: uri

       The requested uri.

    .. attribute:: path

       The path portion of `uri`

    .. attribute:: query

       The query portion of `uri`

    .. attribute:: version

       HTTP version specified in request, e.g. "HTTP/1.1"

    .. attribute:: headers

       `HTTPHeader` dictionary-like object for request headers.  Acts like
       a case-insensitive dictionary with additional methods for repeated
       headers.

    .. attribute:: body

       Request body, if present.

    .. attribute:: remote_ip

       Client's IP address as a string.  If `HTTPServer.xheaders` is set,
       will pass along the real IP address provided by a load balancer
       in the ``X-Real-Ip`` header

    .. attribute:: protocol

       The protocol used, either "http" or "https".  If `HTTPServer.xheaders`
       is seet, will pass along the protocol used by a load balancer if
       reported via an ``X-Scheme`` header.

    .. attribute:: host

       The requested hostname, usually taken from the ``Host`` header.

    .. attribute:: arguments

       GET/POST arguments are available in the arguments property, which
       maps arguments names to lists of values (to support multiple values
       for individual names). Names and values are both unicode always.

    .. attribute:: files

       File uploads are available in the files property, which maps file
       names to list of files. Each file is a dictionary of the form
       {"filename":..., "content_type":..., "body":...}. The content_type
       comes from the provided HTTP header and should not be trusted
       outright given that it can be easily forged.

    .. attribute:: connection

       An HTTP request is attached to a single HTTP connection, which can
       be accessed through the "connection" attribute. Since connections
       are typically kept open in HTTP/1.1, multiple requests can be handled
       sequentially on a single connection.
    """
    def __init__(self, method, uri, version="HTTP/1.0", headers=None,
                 body=None, remote_ip=None, protocol=None, host=None,
                 files=None, connection=None):
        self.method = method
        self.uri = uri
        self.version = version
        self.headers = headers or httputil.HTTPHeaders()
        self.body = body or ""
        if connection and connection.xheaders:
            # Squid uses X-Forwarded-For, others use X-Real-Ip
            self.remote_ip = self.headers.get(
                "X-Real-Ip", self.headers.get("X-Forwarded-For", remote_ip))
            # AWS uses X-Forwarded-Proto
            self.protocol = self.headers.get(
                "X-Scheme", self.headers.get("X-Forwarded-Proto", protocol))
            if self.protocol not in ("http", "https"):
                self.protocol = "http"
        else:
            self.remote_ip = remote_ip
            if protocol:
                self.protocol = protocol
            elif connection and isinstance(connection.stream, 
                                           iostream.SSLIOStream):
                self.protocol = "https"
            else:
                self.protocol = "http"
        self.host = host or self.headers.get("Host") or "127.0.0.1"
        self.files = files or {}
        self.connection = connection
        self._start_time = time.time()
        self._finish_time = None

        scheme, netloc, path, query, fragment = urlparse.urlsplit(native_str(uri))
        self.path = path
        self.query = query
        arguments = parse_qs_bytes(query)
        self.arguments = {}
        for name, values in arguments.iteritems():
            values = [v for v in values if v]
            if values: self.arguments[name] = values

    def supports_http_1_1(self):
        """Returns True if this request supports HTTP/1.1 semantics"""
        return self.version == "HTTP/1.1"

    def write(self, chunk):
        """Writes the given chunk to the response stream."""
        assert isinstance(chunk, bytes_type)
        self.connection.write(chunk)

    def finish(self):
        """Finishes this HTTP request on the open connection."""
        self.connection.finish()
        self._finish_time = time.time()

    def full_url(self):
        """Reconstructs the full URL for this request."""
        return self.protocol + "://" + self.host + self.uri

    def request_time(self):
        """Returns the amount of time it took for this request to execute."""
        if self._finish_time is None:
            return time.time() - self._start_time
        else:
            return self._finish_time - self._start_time

    def get_ssl_certificate(self):
        """Returns the client's SSL certificate, if any.

        To use client certificates, the HTTPServer must have been constructed
        with cert_reqs set in ssl_options, e.g.::

            server = HTTPServer(app,
                ssl_options=dict(
                    certfile="foo.crt",
                    keyfile="foo.key",
                    cert_reqs=ssl.CERT_REQUIRED,
                    ca_certs="cacert.crt"))

        The return value is a dictionary, see SSLSocket.getpeercert() in
        the standard library for more details.
        http://docs.python.org/library/ssl.html#sslsocket-objects
        """
        try:
            return self.connection.stream.socket.getpeercert()
        except ssl.SSLError:
            return None

    def __repr__(self):
        attrs = ("protocol", "host", "method", "uri", "version", "remote_ip",
                 "body")
        args = ", ".join(["%s=%r" % (n, getattr(self, n)) for n in attrs])
        return "%s(%s, headers=%s)" % (
            self.__class__.__name__, args, dict(self.headers))
