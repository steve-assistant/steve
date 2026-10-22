'use strict';
// Steve's loading script, run before a local server's own code
// (06-modules.md, section 7). The server's container has no network; its
// one way out is the pipe named in STEVE_PIPE, where Steve's broker takes
// one line per connection, the host and port wanted, and then checks the
// TLS handshake that follows against it (core/sandbox/relay.py). This
// sends Node's own connections there: `net` and `tls`, which `http`,
// `https` and `fetch` use. It is a convenience, not the wall: code that
// gets round it finds no network at all.
//
// It also takes the server's home off Node's search for a module. Node
// looks in `.node_modules` and `.node_libraries` under USERPROFILE once a
// name is not in the tree, and that folder is the server's home, which it
// may write: a file it put there would be loaded at its next start, past
// the hash its installed tree is checked against (core/sandbox/servers.py).
// Node reads those folders once, as it starts; they are read again here
// without the home, before the server's own code runs. The home stays
// where the server keeps its sign-in.

const Module = require('node:module');
const net = require('node:net');
const tls = require('node:tls');

const HOME = process.env.USERPROFILE;
delete process.env.USERPROFILE;
Module._initPaths();
if (HOME !== undefined) process.env.USERPROFILE = HOME;

const PIPE = process.env.STEVE_PIPE;
const connectSocket = net.Socket.prototype.connect;
const connectTls = tls.connect;

function target(args) {
  // net's own forms: (options[, listener]), (port[, host][, listener]), (path[, listener]).
  const first = args[0];
  if (first !== null && typeof first === 'object') {
    if (first.path !== undefined) return null;
    return { host: first.host || 'localhost', port: Number(first.port) };
  }
  if (typeof first === 'string' && !/^\d+$/.test(first)) return null;
  return { host: typeof args[1] === 'string' ? args[1] : 'localhost', port: Number(first) };
}

function listener(args) {
  return typeof args[args.length - 1] === 'function' ? args[args.length - 1] : undefined;
}

function viaPipe(socket, to, done) {
  connectSocket.call(socket, { path: PIPE }, done);
  // Written before the pipe opens, so it leads whatever the caller writes.
  socket.write(`${String(to.host).replace(/\.$/, '')}:${to.port}\n`);
  return socket;
}

if (PIPE) {
  net.Socket.prototype.connect = function connect(...args) {
    const to = target(args);
    if (to === null) return connectSocket.apply(this, args);
    if (this instanceof tls.TLSSocket) {
      throw new Error('steve: a TLS socket connects only through tls.connect');
    }
    return viaPipe(this, to, listener(args));
  };

  tls.connect = function connect(...args) {
    let options = args[0];
    if (options === null || typeof options !== 'object') {
      const port = args[0];
      const host = typeof args[1] === 'string' ? args[1] : undefined;
      const rest = args.find((a, i) => i > 0 && a !== null && typeof a === 'object') || {};
      options = { ...rest, port, host };
    }
    if (options.socket) return connectTls.apply(this, args);
    const host = options.host || 'localhost';
    const raw = viaPipe(new net.Socket(), { host, port: Number(options.port) || 443 });
    const servername = options.servername === undefined ? host : options.servername;
    return connectTls({ ...options, socket: raw, servername }, listener(args));
  };
}
