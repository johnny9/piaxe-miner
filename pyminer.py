# The MIT License (MIT)
#
# Copyright (c) 2014 Richard Moore
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

import json, socket, sys, threading, time
from urllib import parse as urlparse
from shared import shared
#from cpu_miner import miner
from piaxe import miner
#import cpu_miner
import logging
import piaxe
import signal
import os
import datetime

# Define a global variable to enable or disable file logging
LOG_FILE_ENABLED = False #True

if LOG_FILE_ENABLED:
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_filename = f"pyminer_{timestamp}.log"
    logging.basicConfig(level=logging.DEBUG,
                        format='%(asctime)s - %(levelname)s - %(message)s',
                        filename=log_filename,
                        filemode='w')
else:
    logging.basicConfig(level=logging.DEBUG,
                        format='%(asctime)s - %(levelname)s - %(message)s')


piaxeMiner = miner.BM1366Miner()
piaxeMiner.init()

USER_AGENT = piaxeMiner.get_name()
VERSION = [0, 1]

def human_readable_hashrate(hashrate):
  '''Returns a human readable representation of hashrate.'''

  if hashrate < 1000:
    return '%2f hashes/s' % hashrate
  if hashrate < 10000000:
    return '%2f khashes/s' % (hashrate / 1000)
  if hashrate < 10000000000:
    return '%2f Mhashes/s' % (hashrate / 1000000)
  return '%2f Ghashes/s' % (hashrate / 1000000000)


# Subscription state
class Subscription(object):
  '''Encapsulates the Subscription state from the JSON-RPC server'''

  _max_nonce = 0x7fffffff

  # Subclasses should override this
  def ProofOfWork(header):
    raise Exception('Do not use the Subscription class directly, subclass it')

  class StateException(Exception): pass

  def __init__(self):
    self._id = None
    self._difficulty = None
    self._extranonce1 = None
    self._extranonce2_size = None
    self._worker_name = None

    self._mining_thread = None

  # Accessors
  id = property(lambda s: s._id)
  worker_name = property(lambda s: s._worker_name)

  difficulty = property(lambda s: s._difficulty)

  extranonce1 = property(lambda s: s._extranonce1)
  extranonce2_size = property(lambda s: s._extranonce2_size)


  def set_worker_name(self, worker_name):
    if self._worker_name:
      raise self.StateException('Already authenticated as %r (requesting %r)' % (self._username, username))

    self._worker_name = worker_name


  def set_subscription(self, subscription_id, extranonce1, extranonce2_size):
    if self._id is not None:
      raise self.StateException('Already subscribed')

    self._id = subscription_id
    self._extranonce1 = extranonce1
    self._extranonce2_size = extranonce2_size


  def create_job(self, job_id, prevhash, coinb1, coinb2, merkle_branches, version, nbits, ntime):
    '''Creates a new Job object populated with all the goodness it needs to mine.'''

    if self._id is None:
      raise self.StateException('Not subscribed')

    return piaxe.miner.Job(
      job_id=job_id,
      prevhash=prevhash,
      coinb1=coinb1,
      coinb2=coinb2,
      merkle_branches=merkle_branches,
      version=version,
      nbits=nbits,
      ntime=ntime,
      extranonce1=self._extranonce1,
      extranonce2_size=self.extranonce2_size,
      max_nonce=self._max_nonce,
    )

  def __str__(self):
    return '<Subscription id=%s, extranonce1=%s, extranonce2_size=%d, difficulty=%d worker_name=%s>' % (self.id, self.extranonce1, self.extranonce2_size, self.difficulty, self.worker_name)

class SubscriptionSHA256D(Subscription):
  '''Subscription for Double-SHA256-based coins, like Bitcoin.'''

  ProofOfWork = shared.sha256d

class SimpleJsonRpcClient(object):
  '''Simple JSON-RPC client.

    To use this class:
      1) Create a sub-class
      2) Override handle_reply(self, request, reply)
      3) Call connect(socket)

    Use self.send(method, params) to send JSON-RPC commands to the server.

    A new thread is created for listening to the connection; so calls to handle_reply
    are synchronized. It is safe to call send from withing handle_reply.
  '''

  class ClientException(Exception): pass

  class RequestReplyException(Exception):
    def __init__(self, message, reply, request = None):
      Exception.__init__(self, message)
      self._reply = reply
      self._request = request

    request = property(lambda s: s._request)
    reply = property(lambda s: s._reply)

  class RequestReplyWarning(RequestReplyException):
    '''Sub-classes can raise this to inform the user of JSON-RPC server issues.'''
    pass

  def __init__(self):
    self._socket = None
    self._lock = threading.RLock()
    self._rpc_thread = None
    self._message_id = 1
    self._requests = dict()
    self.error_event = threading.Event()

  def stop(self):
    self.error_event.set()

    try:
        if self._socket:
          self._socket.shutdown(socket.SHUT_RDWR)
          self._socket.close()
    except OSError as e:
        print(f"Error when closing socket: {e}")

    if self._rpc_thread:
      logging.debug("joining rpc_thread")
      self._rpc_thread.join()
      logging.debug("joining done")
      self._rpc_thread = None

  def _handle_incoming_rpc(self):
    data = ""
    while not self.error_event.is_set():
      try:
        # Get the next line if we have one, otherwise, read and block
        if '\n' in data:
          (line, data) = data.split('\n', 1)
        else:
          try:
            chunk = self._socket.recv(1024)
            if not chunk:
              raise Exception("tcp connection closed ...")
            chunk = chunk.decode('utf-8')
            data += chunk
            continue
          except socket.timeout:
            # we have to handle it but we don't care actually
            continue

        if log_protocol:
          logging.debug('JSON-RPC Server > ' + line)

        # Parse the JSON
        try:
          reply = json.loads(line)
        except Exception as e:
          logging.error("JSON-RPC Error: Failed to parse JSON %r (skipping)" % line)
          continue

        try:
          request = None
          with self._lock:
            if 'id' in reply and reply['id'] in self._requests:
              request = self._requests[reply['id']]
            self.handle_reply(request = request, reply = reply)
        except self.RequestReplyWarning as e:
          output = str(e)
          if e.request:
            try:
              output += '\n  ' + e.request
            except TypeError:
              output += '\n  ' + str(e.request)
          output += '\n  ' + str(e.reply)
          logging.error(output)
      except Exception as e:
        logging.error('Exception in RPC thread: %s' % str(e))
        self.error_event.set()
    logging.error("error flag set ... ending handle_incoming_rpc thread")


  def handle_reply(self, request, reply):
    # Override this method in sub-classes to handle a message from the server
    raise self.RequestReplyWarning('Override this method')


  def send(self, method, params):
    '''Sends a message to the JSON-RPC server'''

    try:
      if not self._socket:
        raise self.ClientException('Not connected')

      request = dict(id = self._message_id, method = method, params = params)
      message = json.dumps(request)
      with self._lock:
        self._requests[self._message_id] = request
        self._message_id += 1
        self._socket.send((message + '\n').encode('utf-8'))

      if log_protocol:
        logging.debug('JSON-RPC Server < ' + message)

      return True
    except socket.timeout:
      logging.error("timeout on send: %s" % str(e))
      self.error_event.set()
    except Exception as e:
      logging.error('Exception in send: %s' % str(e))
      self.error_event.set()
      return False

  def mining_submit(self, result):
    params = [ self._subscription.worker_name ] + [ result[k] for k in ('job_id', 'extranonce2', 'ntime', 'nonce', 'version') ]
    return self.send(method = 'mining.submit', params = params)

  def connect(self, socket):
    '''Connects to a remote JSON-RPC server'''

    if self._rpc_thread:
      raise self.ClientException('Already connected')

    self._socket = socket
    # submit sometimes would hang forever
    self._socket.settimeout(10)

    self._rpc_thread = threading.Thread(target = self._handle_incoming_rpc)
    self._rpc_thread.daemon = True
    self._rpc_thread.start()


# Miner client
class Miner(SimpleJsonRpcClient):
  '''Simple mining client'''

  class MinerWarning(SimpleJsonRpcClient.RequestReplyWarning):
    def __init__(self, message, reply, request = None):
      SimpleJsonRpcClient.RequestReplyWarning.__init__(self, 'Mining Sate Error: ' + message, reply, request)

  class MinerAuthenticationException(SimpleJsonRpcClient.RequestReplyException): pass

  def __init__(self, url, username, password, miner):
    SimpleJsonRpcClient.__init__(self)

    self._url = url
    self._username = username
    self._password = password

    self._subscription = SubscriptionSHA256D()

    self._job = None

    self._miner = miner
    self._miner.set_submit_callback(self.mining_submit)

    self._accepted_shares = 0

  # Accessors
  url = property(lambda s: s._url)
  username = property(lambda s: s._username)
  password = property(lambda s: s._password)


  # Overridden from SimpleJsonRpcClient
  def handle_reply(self, request, reply):

    # New work, stop what we were doing before, and start on this.
    if reply.get('method') == 'mining.notify':
      if 'params' not in reply or len(reply['params']) != 9:
        raise self.MinerWarning('Malformed mining.notify message', reply)

      (job_id, prevhash, coinb1, coinb2, merkle_branches, version, nbits, ntime, clean_jobs) = reply['params']

      # Create the new job
      self._job = self._subscription.create_job(
        job_id = job_id,
        prevhash = prevhash,
        coinb1 = coinb1,
        coinb2 = coinb2,
        merkle_branches = merkle_branches,
        version = version,
        nbits = nbits,
        ntime = ntime
      )
      self._miner.start_job(self._job)

      logging.debug('New job: job_id=%s' % job_id)

    # The server wants us to change our difficulty (on all *future* work)
    elif reply.get('method') == 'mining.set_difficulty':
      if 'params' not in reply or len(reply['params']) != 1:
        raise self.MinerWarning('Malformed mining.set_difficulty message', reply)

      (difficulty, ) = reply['params']
      self._miner.set_difficulty(difficulty)

      logging.debug('Change difficulty: difficulty=%s' % difficulty)

    # This is a reply to...
    elif request:

      # ...subscribe; set-up the work and request authorization
      if request.get('method') == 'mining.subscribe':
        if 'result' not in reply or len(reply['result']) != 3:
          raise self.MinerWarning('Reply to mining.subscribe is malformed', reply, request)

        (tmp, extranonce1, extranonce2_size) = reply['result']

        if not isinstance(tmp, list) or len(tmp) != 1 or not isinstance(tmp[0], list) or not len(tmp[0]) == 2:
          raise self.MinerWarning('Reply to mining.subscribe is malformed', reply, request)

        (mining_notify, subscription_id) = tmp[0]

        self._subscription.set_subscription(subscription_id, extranonce1, extranonce2_size)

        logging.debug('Subscribed: subscription_id=%s' % subscription_id)

        # Request authentication
        self.send(method = 'mining.authorize', params = [ self.username, self.password ])

      # ...authorize; if we failed to authorize, quit
      elif request.get('method') == 'mining.authorize':
        if 'result' not in reply or not reply['result']:
          raise self.MinerAuthenticationException('Failed to authenticate worker', reply, request)

        worker_name = request['params'][0]
        self._subscription.set_worker_name(worker_name)

        logging.debug('Authorized: worker_name=%s' % worker_name)

      # ...submit; complain if the server didn't accept our submission
      elif request.get('method') == 'mining.submit':
        if 'result' not in reply or not reply['result']:
          logging.info('Share - Invalid')
          self._miner.not_accepted_callback()
          raise self.MinerWarning('Failed to accept submit', reply, request)

        self._accepted_shares += 1
        self._miner.accepted_callback()
        logging.info('Accepted shares: %d' % self._accepted_shares)

      # ??? *shrug*
      else:
        raise self.MinerWarning('Unhandled message', reply, request)

    # ??? *double shrug*
    else:
      raise self.MinerWarning('Bad message state', reply)

  def serve(self):
    '''Begins the miner. This method does not return.'''

    # Figure out the hostname and port
    url = urlparse.urlparse(self.url)
    hostname = url.hostname or ''
    port = url.port or 9333

    logging.info('Starting server on %s:%d' % (hostname, port))
    # clear error if there was any
    self.error_event.clear()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((hostname, port))
    self.connect(sock)

    self.send(method = 'mining.subscribe', params = [ "%s/%s" % (USER_AGENT, '.'.join(str(p) for p in VERSION)) ])

def sigint_handler(signal_received, frame):
    print('SIGINT (Ctrl+C) captured, exiting gracefully')
    piaxeMiner.shutdown()
    os._exit(0)


# CLI for cpu mining
if __name__ == '__main__':
  import argparse

  # Parse the command line
  parser = argparse.ArgumentParser(description="PyMiner is a Stratum CPU mining client. "
                                               "If you like this piece of software, please "
                                               "consider supporting its future development via "
                                               "donating to one of the addresses indicated in the "
                                               "README.md file")

  parser.add_argument('-o', '--url', help = 'stratum mining server url (eg: stratum+tcp://foobar.com:3333)')
  parser.add_argument('-u', '--user', dest = 'username', default = '', help = 'username for mining server', metavar = "USERNAME")
  parser.add_argument('-p', '--pass', dest = 'password', default = '', help = 'password for mining server', metavar = "PASSWORD")

  parser.add_argument('-O', '--userpass', help = 'username:password pair for mining server', metavar = "USERNAME:PASSWORD")

  parser.add_argument('-B', '--background', action ='store_true', help = 'run in the background as a daemon')

  parser.add_argument('-q', '--quiet', action ='store_true', help = 'suppress non-errors')
  parser.add_argument('-P', '--dump-protocol', dest = 'protocol', action ='store_true', help = 'show all JSON-RPC chatter')
  parser.add_argument('-d', '--debug', action ='store_true', help = 'show extra debug information')

  parser.add_argument('-v', '--version', action = 'version', version = '%s/%s' % (USER_AGENT, '.'.join(str(v) for v in VERSION)))

  options = parser.parse_args(sys.argv[1:])

  message = None

  # Get the username/password
  username = options.username
  password = options.password

  if options.userpass:
    if username or password:
      message = 'May not use -O/-userpass in conjunction with -u/--user or -p/--pass'
    else:
      try:
        (username, password) = options.userpass.split(':')
      except Exception as e:
        message = 'Could not parse username:password for -O/--userpass'

  # Was there an issue? Show the help screen and exit.
  if message:
    parser.print_help()
    print()
    print(message)
    sys.exit(1)

  log_level = logging.INFO

  if options.debug:
    log_level = logging.DEBUG

  log_protocol = options.protocol

  # Configure logging with time


  # The want a daemon, give them a daemon
  if options.background:
    import os
    if os.fork() or os.fork(): sys.exit()

  signal.signal(signal.SIGINT, sigint_handler)

  # Heigh-ho, heigh-ho, it's off to work we go...

  while True:
    try:
      miner = Miner(options.url, username, password, piaxeMiner)
      miner.serve()
    except Exception as e:
      logging.error("exception in serve ... restarting client")
      miner.error_event.set()

    logging.debug("waiting for error")
    miner.error_event.wait()
    logging.debug("error received")
    miner.stop()
    time.sleep(5)


