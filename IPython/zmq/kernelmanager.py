"""Kernel frontend classes.

TODO: Create logger to handle debugging and console messages.

"""

# Standard library imports.
from Queue import Queue, Empty
from subprocess import Popen
from threading import Thread
import time
import traceback

# System library imports.
import zmq
from zmq import POLLIN, POLLOUT, POLLERR
from zmq.eventloop import ioloop

# Local imports.
from IPython.utils.traitlets import HasTraits, Any, Bool, Int, Instance, Str, \
    Type
from kernel import launch_kernel
from session import Session

# Constants.
LOCALHOST = '127.0.0.1'



class ZmqSocketChannel(Thread):
    """ The base class for the channels that use ZMQ sockets.
    """
    context = None
    session = None
    socket = None
    ioloop = None
    iostate = None

    def __init__(self, context, session, address=None):
        super(ZmqSocketChannel, self).__init__()
        self.daemon = True

        self.context = context
        self.session = session
        self.address = address

    def stop(self):
        """Stop the thread's activity. Returns when the thread terminates.

        The thread will raise :class:`RuntimeError` if :method:`self.start`
        is called again.
        """
        self.join()


    def get_address(self):
        """ Get the channel's address. By the default, a channel is on 
            localhost with no port specified (a negative port number).
        """
        return self._address

    def set_adresss(self, address):
        """ Set the channel's address. Should be a tuple of form:
                (ip address [str], port [int]).
            or None, in which case the address is reset to its default value.
        """
        # FIXME: Validate address.
        if self.is_alive():  # This is Thread.is_alive
            raise RuntimeError("Cannot set address on a running channel!")
        else:
            if address is None:
                address = (LOCALHOST, 0)
            self._address = address

    address = property(get_address, set_adresss)

    def add_io_state(self, state):
        """Add IO state to the eventloop.

        This is thread safe as it uses the thread safe IOLoop.add_callback.
        """
        def add_io_state_callback():
            if not self.iostate & state:
                self.iostate = self.iostate | state
                self.ioloop.update_handler(self.socket, self.iostate)
        self.ioloop.add_callback(add_io_state_callback)

    def drop_io_state(self, state):
        """Drop IO state from the eventloop.

        This is thread safe as it uses the thread safe IOLoop.add_callback.
        """
        def drop_io_state_callback():
            if self.iostate & state:
                self.iostate = self.iostate & (~state)
                self.ioloop.update_handler(self.socket, self.iostate)
        self.ioloop.add_callback(drop_io_state_callback)


class SubSocketChannel(ZmqSocketChannel):

    def __init__(self, context, session, address=None):
        super(SubSocketChannel, self).__init__(context, session, address)

    def run(self):
        self.socket = self.context.socket(zmq.SUB)
        self.socket.setsockopt(zmq.SUBSCRIBE,'')
        self.socket.setsockopt(zmq.IDENTITY, self.session.session)
        self.socket.connect('tcp://%s:%i' % self.address)
        self.ioloop = ioloop.IOLoop()
        self.iostate = POLLIN|POLLERR
        self.ioloop.add_handler(self.socket, self._handle_events, 
                                self.iostate)
        self.ioloop.start()

    def stop(self):
        self.ioloop.stop()
        super(SubSocketChannel, self).stop()

    def _handle_events(self, socket, events):
        # Turn on and off POLLOUT depending on if we have made a request
        if events & POLLERR:
            self._handle_err()
        if events & POLLIN:
            self._handle_recv()

    def _handle_err(self):
        # We don't want to let this go silently, so eventually we should log.
        raise zmq.ZMQError()

    def _handle_recv(self):
        # Get all of the messages we can
        while True:
            try:
                msg = self.socket.recv_json(zmq.NOBLOCK)
            except zmq.ZMQError:
                # Check the errno?
                # Will this tigger POLLERR?
                break
            else:
                self.call_handlers(msg)

    def call_handlers(self, msg):
        """This method is called in the ioloop thread when a message arrives.

        Subclasses should override this method to handle incoming messages.
        It is important to remember that this method is called in the thread
        so that some logic must be done to ensure that the application leve
        handlers are called in the application thread.
        """
        raise NotImplementedError('call_handlers must be defined in a subclass.')

    def flush(self, timeout=1.0):
        """Immediately processes all pending messages on the SUB channel.

        This method is thread safe.

        Parameters
        ----------
        timeout : float, optional
            The maximum amount of time to spend flushing, in seconds. The
            default is one second.
        """
        # We do the IOLoop callback process twice to ensure that the IOLoop
        # gets to perform at least one full poll.
        stop_time = time.time() + timeout
        for i in xrange(2):
            self._flushed = False
            self.ioloop.add_callback(self._flush)
            while not self._flushed and time.time() < stop_time:
                time.sleep(0.01)
        
    def _flush(self):
        """Called in this thread by the IOLoop to indicate that all events have
        been processed.
        """
        self._flushed = True


class XReqSocketChannel(ZmqSocketChannel):

    command_queue = None

    def __init__(self, context, session, address=None):
        self.command_queue = Queue()
        super(XReqSocketChannel, self).__init__(context, session, address)

    def run(self):
        self.socket = self.context.socket(zmq.XREQ)
        self.socket.setsockopt(zmq.IDENTITY, self.session.session)
        self.socket.connect('tcp://%s:%i' % self.address)
        self.ioloop = ioloop.IOLoop()
        self.iostate = POLLERR|POLLIN
        self.ioloop.add_handler(self.socket, self._handle_events, 
                                self.iostate)
        self.ioloop.start()

    def stop(self):
        self.ioloop.stop()
        super(XReqSocketChannel, self).stop()

    def _handle_events(self, socket, events):
        if events & POLLERR:
            self._handle_err()
        if events & POLLOUT:
            self._handle_send()
        if events & POLLIN:
            self._handle_recv()

    def _handle_recv(self):
        msg = self.socket.recv_json()
        self.call_handlers(msg)

    def _handle_send(self):
        try:
            msg = self.command_queue.get(False)
        except Empty:
            pass
        else:
            self.socket.send_json(msg)
        if self.command_queue.empty():
            self.drop_io_state(POLLOUT)

    def _handle_err(self):
        # We don't want to let this go silently, so eventually we should log.
        raise zmq.ZMQError()

    def _queue_request(self, msg, callback):
        self.command_queue.put(msg)
        self.add_io_state(POLLOUT)

    def call_handlers(self, msg):
        """This method is called in the ioloop thread when a message arrives.

        Subclasses should override this method to handle incoming messages.
        It is important to remember that this method is called in the thread
        so that some logic must be done to ensure that the application leve
        handlers are called in the application thread.
        """
        raise NotImplementedError('call_handlers must be defined in a subclass.')

    def execute(self, code, callback=None):
        # Create class for content/msg creation. Related to, but possibly
        # not in Session.
        content = dict(code=code)
        msg = self.session.msg('execute_request', content)
        self._queue_request(msg, callback)
        return msg['header']['msg_id']

    def complete(self, text, line, block=None, callback=None):
        content = dict(text=text, line=line)
        msg = self.session.msg('complete_request', content)
        self._queue_request(msg, callback)
        return msg['header']['msg_id']

    def object_info(self, oname, callback=None):
        content = dict(oname=oname)
        msg = self.session.msg('object_info_request', content)
        self._queue_request(msg, callback)
        return msg['header']['msg_id']


class RepSocketChannel(ZmqSocketChannel):

    def on_raw_input(self):
        pass


class KernelManager(HasTraits):
    """ Manages a kernel for a frontend.

    The SUB channel is for the frontend to receive messages published by the
    kernel.
        
    The REQ channel is for the frontend to make requests of the kernel.
    
    The REP channel is for the kernel to request stdin (raw_input) from the
    frontend.
    """

    # Whether the kernel manager is currently listening on its channels.
    is_listening = Bool(False)

    # The PyZMQ Context to use for communication with the kernel.
    context = Instance(zmq.Context, ())

    # The Session to use for communication with the kernel.
    session = Instance(Session, ())

    # The classes to use for the various channels.
    sub_channel_class = Type(SubSocketChannel)
    xreq_channel_class = Type(XReqSocketChannel)
    rep_channel_class = Type(RepSocketChannel)
    
    # Protected traits.
    _kernel = Instance(Popen)
    _sub_channel = Any
    _xreq_channel = Any
    _rep_channel = Any

    #--------------------------------------------------------------------------
    # Channel management methods:
    #--------------------------------------------------------------------------

    def start_listening(self):
        """Starts listening on the specified ports. If already listening, raises
        a RuntimeError.
        """
        if self.is_listening:
            raise RuntimeError("Cannot start listening. Already listening!")
        else:
            self.is_listening = True
            self.sub_channel.start()
            self.xreq_channel.start()
            self.rep_channel.start()

    @property
    def is_alive(self):
        """ Returns whether the kernel is alive. """
        if self.is_listening:
            # TODO: check if alive.
            return True
        else:
            return False

    def stop_listening(self):
        """Stops listening. If not listening, does nothing. """
        if self.is_listening:
            self.is_listening = False
            self.sub_channel.stop()
            self.xreq_channel.stop()
            self.rep_channel.stop()

    #--------------------------------------------------------------------------
    # Kernel process management methods:
    #--------------------------------------------------------------------------

    def start_kernel(self):
        """Starts a kernel process and configures the manager to use it.

        If ports have been specified via the address attributes, they are used.
        Otherwise, open ports are chosen by the OS and the channel port
        attributes are configured as appropriate.
        """
        xreq, sub = self.xreq_address, self.sub_address
        if xreq[0] != LOCALHOST or sub[0] != LOCALHOST:
            raise RuntimeError("Can only launch a kernel on localhost."
                               "Make sure that the '*_address' attributes are "
                               "configured properly.")

        kernel, xrep, pub = launch_kernel(xrep_port=xreq[1], pub_port=sub[1])
        self.set_kernel(kernel)
        self.xreq_address = (LOCALHOST, xrep)
        self.sub_address = (LOCALHOST, pub)

    def set_kernel(self, kernel):
        """Sets the kernel manager's kernel to an existing kernel process.

        It is *not* necessary to a set a kernel to communicate with it via the
        channels, and those objects must be configured separately. It
        *is* necessary to set a kernel if you want to use the manager (or
        frontends that use the manager) to signal and/or kill the kernel.

        Parameters:
        -----------
        kernel : Popen
            An existing kernel process.
        """
        self._kernel = kernel

    @property
    def has_kernel(self):
        """Returns whether a kernel process has been specified for the kernel
        manager.

        A kernel process can be set via 'start_kernel' or 'set_kernel'.
        """
        return self._kernel is not None

    def kill_kernel(self):
        """ Kill the running kernel. """
        if self._kernel:
            self._kernel.kill()
            self._kernel = None
        else:
            raise RuntimeError("Cannot kill kernel. No kernel is running!")

    def signal_kernel(self, signum):
        """ Sends a signal to the kernel. """
        if self._kernel:
            self._kernel.send_signal(signum)
        else:
            raise RuntimeError("Cannot signal kernel. No kernel is running!")

    #--------------------------------------------------------------------------
    # Channels used for communication with the kernel:
    #--------------------------------------------------------------------------

    @property
    def sub_channel(self):
        """Get the SUB socket channel object."""
        if self._sub_channel is None:
            self._sub_channel = self.sub_channel_class(self.context,
                                                       self.session)
        return self._sub_channel

    @property
    def xreq_channel(self):
        """Get the REQ socket channel object to make requests of the kernel."""
        if self._xreq_channel is None:
            self._xreq_channel = self.xreq_channel_class(self.context, 
                                                         self.session)
        return self._xreq_channel

    @property
    def rep_channel(self):
        """Get the REP socket channel object to handle stdin (raw_input)."""
        if self._rep_channel is None:
            self._rep_channel = self.rep_channel_class(self.context, 
                                                       self.session)
        return self._rep_channel

    #--------------------------------------------------------------------------
    # Delegates for the Channel address attributes:
    #--------------------------------------------------------------------------

    def get_sub_address(self):
        return self.sub_channel.address

    def set_sub_address(self, address):
        self.sub_channel.address = address

    sub_address = property(get_sub_address, set_sub_address,
                           doc="The address used by SUB socket channel.")

    def get_xreq_address(self):
        return self.xreq_channel.address

    def set_xreq_address(self, address):
        self.xreq_channel.address = address

    xreq_address = property(get_xreq_address, set_xreq_address,
                            doc="The address used by XREQ socket channel.")
    
    def get_rep_address(self):
        return self.rep_channel.address

    def set_rep_address(self, address):
        self.rep_channel.address = address

    rep_address = property(get_rep_address, set_rep_address,
                           doc="The address used by REP socket channel.")
    
