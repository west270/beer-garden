# -*- coding: utf-8 -*-
import logging
import multiprocessing
import signal
from functools import partial
from multiprocessing.connection import Connection, Pipe
from multiprocessing.context import SpawnContext
from multiprocessing.queues import Queue
from types import FrameType
from typing import Any, Callable, TypeVar

from box import Box
from brewtils.models import Event

import beer_garden
import beer_garden.config
import beer_garden.db.api as db
import beer_garden.events.events_manager
import beer_garden.queue.api as queue
from beer_garden.events.processors import CallableProcessor
from beer_garden.log import LogProcessor

T = TypeVar("T", bound="EntryPoint")


class EntryPoint(object):
    """A Beergarden API entry point

    This class represents an entry point into Beergarden.

    To create a new entry point:
    - Make a new subpackage under beer_garden.api
    - In that package's __init__ add a `run` function that will run the entry point
    process and a `signal_handler` function that will stop it
    - Add the new entry point to the configuration spec as a child of the "entry" dict.
    The name of the new dict must match the subpackage name, and the new entry must
    have an `enable` flag as an immediate child.

    Args:
        name: Part of the process name. Full name will be "BGEntryPoint-{name}"
        target: The method that will be called when the process starts
        signal_handler: SIGTERM handler to gracefully shut down the process

    Class Attributes:

    Attributes:
        self._signal_handler: Signal handler function that will be invoked (in the
            entry point process) when a SIGTERM is received
        self._process: The actual entry point process object
        self._tx_conn: Send end of communication pipe. Used to send events into the
            entry point
        self._rx_conn: Receive end of communication pipe. Passed into the entry point
            process where it should be monitored for events.
        upstream_events: Queue used to send events to the main process
    """

    def __init__(
        self,
        name: str,
        target: Callable,
        context: SpawnContext,
        signal_handler: Callable[[int, FrameType], None],
        log_queue: Queue,
        upstream_events: Queue,
    ):
        self._name = name
        self._target = target
        self._context = context
        self._signal_handler = signal_handler
        self._log_queue = log_queue
        self.upstream_events = upstream_events

        self._logger = logging.getLogger(__name__)
        self._process = None
        self._rx_conn, self._tx_conn = Pipe(duplex=False)

    def start(self) -> None:
        """Start the entry point process"""
        process_name = f"BGEntryPoint-{self._name}"

        self._process = self._context.Process(
            target=self._target_wrapper,
            args=(
                beer_garden.config.get(),
                self._log_queue,
                self.upstream_events,
                self._target,
                self._signal_handler,
                self._rx_conn,
            ),
            name=process_name,
            daemon=True,
        )
        self._process.start()

    def stop(self, timeout: int = None) -> None:
        """Stop the process with a SIGTERM

        If a `timeout` is specified this method will wait that long for the process to
        stop gracefully. If the process has still not stopped after the timeout expires
        it will be forcefully terminated with SIGKILL.

        Args:
            timeout: Amount of time to wait for the process to stop

        Returns:
            None
        """
        self._process.terminate()
        self._process.join(timeout=timeout)

        if self._process.exitcode is None:
            self._logger.warning(
                f"Process {self._process.name} is still running - sending SIGKILL"
            )
            self._process.kill()

    def send_event(self, event: Event) -> None:
        """Send an event into the entry point process

        Args:
            event: The event to send

        Returns:
            None
        """
        self._tx_conn.send(event)

    @staticmethod
    def _target_wrapper(
        config: Box,
        log_queue: Queue,
        event_queue: Queue,
        target: Callable,
        signal_handler: Callable[[int, FrameType], None],
        rx_conn: Connection,
    ) -> Any:
        """Helper method that sets up the process environment before calling `target`

        This does several things that are needed by all entry points:
        - Sets up the signal handler function that will be used to terminate the process
        - Sets the global application configuration
        - Configures logging to send all records back to the main application process
        - Creates and registers a connection to the database

        It then calls the actual entry point target, which will be the `run` method in
        the subpackage's __init__.

        Args:
            config:
            log_queue:
            event_queue:
            target:
            signal_handler:
            rx_conn:

        Returns:
            The result of the `target` function
        """
        # Set the process to ignore SIGINT and exit on SIGTERM
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal_handler)

        # First thing to do is set the config
        beer_garden.config.assign(config)

        # Then set up logging to push everything back to the main process
        beer_garden.log.setup_entry_point_logging(log_queue)

        # Also set up plugin logging
        beer_garden.log.load_plugin_log_config()

        # Set up a database connection
        db.create_connection(db_config=beer_garden.config.get("db"))

        # Set up message queue connections
        queue.create_clients(beer_garden.config.get("amq"))

        # Then setup upstream event queue (used to send events to the main process)
        beer_garden.events.events_manager.set_upstream(event_queue)

        # Now invoke the actual process target
        return target(rx_conn)


class Manager:
    """Entry points manager

    This is the way events are coordinated across entry points. This class does a
    couple of things:

    - Has a list of the entry points
    - Sets up a QueueProcessor for each of the entry point's upstream event queues. In
    other words, this class is listening for events coming from every entry point.
    - Allows registration of callbacks for certain types of events.
    - When an event is received from one of the entry point queues it will fire any
        callbacks registered for the event type.

    Attributes:
    """

    context: SpawnContext = None
    log_queue: Queue = None

    def __init__(self):
        self.processors = []
        self.entry_points = []
        self.callbacks = {}

        self.context = multiprocessing.get_context("spawn")
        self.log_queue = self.context.Queue()

        self.log_reader = LogProcessor(name="LogProcessor", queue=self.log_queue)

        for entry_name, entry_value in beer_garden.config.get("entry").items():
            if entry_value.get("enable"):
                self.entry_points.append(self.create(entry_name))

    def create(self, module_name: str) -> T:
        module = getattr(beer_garden.api, module_name)

        return EntryPoint(
            name=module_name,
            target=module.run,
            context=self.context,
            signal_handler=module.signal_handler,
            log_queue=self.log_queue,
            upstream_events=self.context.Queue(),
        )

    def register_callback(self, event_name: str, callback: Callable):
        if event_name not in self.callbacks:
            self.callbacks[event_name] = []
        self.callbacks[event_name].append(callback)

    def start(self):
        self.log_reader.start()

        for entry_point in self.entry_points:
            entry_point.start()

        for ep in self.entry_points:
            call = CallableProcessor(
                queue=ep.upstream_events, callable_obj=partial(self.process)
            )
            call.start()
            self.processors.append(call)

    def stop(self):
        self.log_reader.stop()

        for entry_point in self.entry_points:
            entry_point.stop(timeout=10)

        for processor in self.processors:
            processor.stop()

    def put(self, item, entry_points=None):
        """Publish an event to specified entry points.

        Args:
            item:

        Returns:

        """
        for ep in self.entry_points:
            ep.send_event(item)

    def process(self, item):
        # First fire any callbacks
        if item.name in self.callbacks:
            for callback in self.callbacks[item.name]:
                callback(item)

        # TODO - then send the event to all other queues
